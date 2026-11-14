"""The Chaos Monkey's fault library — every attack is plain, deterministic code.

Two kinds of fault:

* **system** faults break the simulated legacy estate. Red never touches the
  systems' state directly: it calls the systems' chaos hook
  ``POST {SYSTEMS_URL}/{sys}/_chaos/{fault}`` with ``{"params": {...}}`` and the
  systems app (workstream A) applies the effect.
* **agent** faults attack the Hive itself. ``agent_kill`` reads the victim's
  ``pid`` from ``swarm.agents`` and sends a real ``SIGKILL`` — no warning, no
  message to the swarm. Recovery has to come from MongoDB leases + checkpoints.

Each fault is a frozen :class:`Fault` in :data:`FAULTS`; :func:`execute` runs one
and returns a JSON-able result dict (it never raises for an expected failure, so
the runner can always record a ``chaos_events`` document).
"""
from __future__ import annotations

import logging
from pathlib import Path
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
import httpx

from common import config
from common.db import col, now, swarm, world

log = logging.getLogger("chaos.faults")

SYSTEM_TARGETS = tuple(config.SYSTEM_IDS)  # ("mainframe", "unix", "portal")


@dataclass(frozen=True)
class Fault:
    name: str
    kind: str                      # "system" | "agent"
    targets: tuple[str, ...]       # valid targets ("swarm" for agent faults)
    default_target: str
    description: str
    tests: str                     # what it tests in the swarm
    default_params: dict = field(default_factory=dict)
    switch: str | None = None      # env var that must be "1" before this fault may fire (new agent attacks)

    def enabled(self) -> bool:
        return self.switch is None or os.environ.get(self.switch, "").strip().lower() in ("1", "true", "yes", "on")

    def params_with_defaults(self, params: dict | None) -> dict:
        return {**self.default_params, **(params or {})}


FAULTS: dict[str, Fault] = {f.name: f for f in [
    Fault("batch_abort", "system", ("mainframe",), "mainframe",
          "A malformed record ABENDs the nightly NIGHTBAT job (S0C7); unix gets no batch file and the "
          "portal serves stale balances with HTTP 200.",
          "Root-cause tracing across the chain (portal -> unix -> mainframe)."),
    Fault("disk_fill", "system", ("unix",), "unix",
          "The transfer spool fills up; file transfers fail and downstream times out.",
          "Diagnosis from metrics (disk_pct), not from a status flag.",
          {"pct": 97}),
    Fault("cron_stop", "system", ("unix",), "unix",
          "The xfer_pull cron job silently stops — no error is ever logged.",
          "Detecting the absence of activity."),
    Fault("session_leak", "system", ("portal",), "portal",
          "The web portal leaks sessions until latency climbs and logins fail.",
          "Metric-driven diagnosis and a safe, non-destructive fix."),
    Fault("lying_success", "system", SYSTEM_TARGETS, "mainframe",
          "The next N ops on the system return success without applying.",
          "Verification by read-back: the Guard bee must catch ACK != APPLIED.",
          {"count": 1}),
    Fault("slow_apply", "system", SYSTEM_TARGETS, "mainframe",
          f"Ops are accepted but only take effect ~{int(config.SLOW_APPLY_S)} s later.",
          "Telling 'sent' apart from 'took effect'; no blind re-sends."),
    Fault("sqli_exploit", "system", ("portal",), "portal",
          "SQL injection through the unpatched account search; data is tampered.",
          "Security patch + exploit retest."),
    Fault("config_exploit", "system", ("unix",), "unix",
          "A world-writable /etc/xfer.conf is exploited to redirect transfers.",
          "Security patch (chmod) + exploit retest."),
    Fault("schema_drift", "system", ("unix",), "unix",
          "A vendor patch silently renames a field in the unix xfer manifest (balance -> acct_bal); unix stays "
          "healthy, the portal can no longer unmarshal balances and serves stale ones with HTTP 200.",
          "Adapting to a changed layout: detect the field diff, propose and verify a mapping, remember it.",
          {}),
    Fault("false_log", "system", SYSTEM_TARGETS, "mainframe",
          "A 'false comment': plausible log lines (an ABEND, a SOAP fault, an operator note) that blame the wrong "
          "system. Nothing else changes.",
          "Knowing what actually happened: trusting read-back over what the logs claim.",
          {}, switch="RED_FLEET"),
    Fault("credential_exploit", "system", ("mainframe",), "mainframe",
          "An intruder logs on with the factory-default OPERATOR password and moves money in the ledger.",
          "Security incident from logs + probe; destructive fix (rotate_credential) behind an approval.",
          {}, switch="RED_FLEET"),
    Fault("agent_kill", "agent", ("swarm",), "swarm",
          "SIGKILL a live bee process (by id, or the worker that just sent a step).",
          "Lease expiry, Undertaker detection, Nurse takeover from a MongoDB checkpoint.",
          {"selector": {"role": "worker", "phase": "step_sent"}, "wait_s": 30}),
    Fault("lie_to_agent", "system", SYSTEM_TARGETS, "mainframe",
          "Plant a vetted prompt-injection line in system logs, leaving actual state unchanged.",
          "Treat log instructions as data; Guard read-back still decides what applied.", {}, switch="CHAOS_AGENT_LIE"),
    Fault("agent_freeze", "agent", ("swarm",), "swarm",
          "SIGSTOP a live bee for a while, then SIGCONT: the process still exists but stops heartbeating and "
          "acting; when it wakes it holds a lease that is no longer its own.",
          "Lease expiry on a stalled (not dead) process, takeover, and fencing of the zombie's late writes.",
          {"selector": {"role": "worker", "phase": "step_sent"}, "wait_s": 30, "freeze_s": 25},
          switch="CHAOS_AGENT_FREEZE"),
    Fault("poison_memory", "agent", ("swarm",), "swarm",
          "Plant a learned repair aimed at the wrong system (e.g. purge_spool on mainframe for a full unix spool) "
          "where the Hive will recall it; the genuine repair is hidden until reset.",
          "Memory you can't trust: Guard read-back rejects the recalled repair, the playbook is retired, re-plan.",
          {}, switch="CHAOS_POISON_MEMORY"),
]}


class FaultError(ValueError):
    """Unknown fault or invalid target (a caller mistake, not a fault outcome)."""


def get(name: str) -> Fault:
    try:
        f = FAULTS[name]
    except KeyError:
        raise FaultError(f"unknown fault {name!r}; known: {', '.join(FAULTS)}") from None
    if not f.enabled():
        raise FaultError(f"{name} is switched off (set {f.switch}=1)")
    return f


def resolve_target(fault: Fault, target: str | None) -> str:
    target = target or fault.default_target
    if target not in fault.targets:
        raise FaultError(f"{fault.name} cannot target {target!r}; valid: {', '.join(fault.targets)}")
    return target


# ------------------------------------------------------------------ system faults: the chaos hook
_http_lock = threading.Lock()
_http: httpx.Client | None = None


def http_client() -> httpx.Client:
    """Shared, thread-safe HTTP client to the systems app (tests may swap it via set_http_client)."""
    global _http
    with _http_lock:
        if _http is None:
            _http = httpx.Client(timeout=5.0)
        return _http


def set_http_client(client: httpx.Client | None) -> None:
    global _http
    with _http_lock:
        _http = client


def _post(path: str, body: dict) -> dict:
    url = config.SYSTEMS_URL.rstrip("/") + path   # read at call time: tests point it elsewhere
    try:
        r = http_client().post(url, json=body)
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "url": url}
    try:
        data = r.json()
    except ValueError:
        data = {"text": r.text[:200]}
    if not isinstance(data, dict):
        data = {"response": data}
    ok = r.is_success and data.get("ok", True) is not False
    return {"ok": ok, "http_status": r.status_code, **{k: v for k, v in data.items() if k != "ok"}}


def hit_system(fault: str, target: str, params: dict) -> dict:
    """POST /{sys}/_chaos/{fault} {"params": {...}} -> {ok, effect, ...}."""
    return _post(f"/{target}/_chaos/{fault}", {"params": params})


def clear_all() -> dict:
    """POST /_chaos/clear — remove every active system fault (not itself a fault event)."""
    return _post("/_chaos/clear", {})


# ------------------------------------------------------------------ agent fault: SIGKILL a bee
DEFAULT_SELECTOR = {"role": "worker", "phase": "step_sent"}


def _fresh_cutoff():
    """An agent is 'alive' if it heartbeated within ~2 leases (heartbeat every HEARTBEAT_S)."""
    return now() - timedelta(seconds=max(2 * config.LEASE_S, 3 * config.HEARTBEAT_S))


def _proc_start_epoch(pid: int) -> float | None:
    """Wall-clock start time of a Linux process (None if unknown). Used to refuse recycled PIDs."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        start_ticks = int(fields[19])  # field 22 overall; 20th after "pid (comm)"
        with open("/proc/stat") as f:
            btime = next(int(line.split()[1]) for line in f if line.startswith("btime"))
        return btime + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _killable(agent: dict) -> str | None:
    """Return a reason the agent must NOT be killed, or None if it's a valid target."""
    pid = agent.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return "no pid"
    if pid in (os.getpid(), os.getppid()):
        return "refusing to kill self/parent"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "process already gone"
    except PermissionError:
        return "not our process"
    started = _proc_start_epoch(pid)
    last_seen = agent.get("last_seen")
    if started and last_seen and started > last_seen.timestamp() + 5:
        return "pid was recycled (process newer than the agent's last heartbeat)"
    return None


def _candidates(query: dict) -> list[dict]:
    # deterministic order (same scenario -> same victim): sorted by agent id
    return list(col("agents").find(query).sort([("_id", 1)]))


# Bumped by runner.cancel() (i.e. a war-room reset): a manual kill still hunting for its victim
# gives up instead of striking a worker in the next round.
_hunt_epoch = 0


def cancel_pending_hunts() -> None:
    global _hunt_epoch
    _hunt_epoch += 1


def _current_night() -> str | None:
    """Id of the chaos run (night) in progress, or None between nights. Read from MongoDB so every
    process agrees: a reset ends the run, and starting a scenario opens a new one."""
    doc = col("runs").find_one({"ended_at": None}, {"_id": 1}, sort=[("started_at", -1)])
    return doc["_id"] if doc else None


SELECTOR_FIELDS = {"agent", "role", "phase", "specialty", "incident", "task", "target"}


def clean_selector(selector) -> dict:
    """Selectors arrive from the war room's HTTP API: allow only known fields with plain string
    values, so a request can never smuggle query operators ($where, $ne, ...) into MongoDB."""
    if not isinstance(selector, dict):
        raise ValueError("selector must be an object")
    bad = [k for k, v in selector.items() if k not in SELECTOR_FIELDS or not isinstance(v, str)]
    if bad:
        raise ValueError(f"unsupported selector field(s) {bad}; allowed: {sorted(SELECTOR_FIELDS)} (string values)")
    return dict(selector)


def find_victim(selector: dict, wait_s: float = 30, poll_s: float = 0.25) -> tuple[dict | None, str, float]:
    """Poll swarm.agents for a live agent matching ``selector``.

    ``{"agent": id}`` targets one bee. Any other keys are a field filter (e.g.
    ``{"role": "worker", "phase": "step_sent"}``) restricted to working agents
    with a fresh heartbeat. After ``wait_s`` without a match the kill gives up
    (``how="gave_up"``) and kills nobody: there is deliberately no fallback to
    "any busy worker", so a late press can never strike a random bee (e.g. spoil
    the round-2 timing). An ``{"agent": id}`` selector that never turns up
    returns ``how="none"``. Returns ``(agent, how, waited_s)``.

    A kill belongs to the night it was requested in: if that night ends (a reset) or another one
    starts before a victim turns up, the hunt is called off (``how="cancelled"``). Unlike the
    in-process reset epoch alone, this holds across processes (war room, ``chaos.cli``), so a stray
    request can never strike a worker in the next round.
    """
    t0, epoch, night = time.monotonic(), _hunt_epoch, _current_night()

    def called_off() -> bool:
        return _hunt_epoch != epoch or _current_night() != night

    if "agent" in selector:
        query = {"_id": selector["agent"], "status": {"$ne": "dead"}}
    else:
        query = {"status": "working", **selector}
    while True:
        fresh = {**query, "last_seen": {"$gte": _fresh_cutoff()}}
        for a in _candidates(fresh):
            if _killable(a) is None:
                if called_off():
                    return None, "cancelled", time.monotonic() - t0
                return a, "selector", time.monotonic() - t0
        if time.monotonic() - t0 >= wait_s:
            break
        time.sleep(poll_s)
        if called_off():
            return None, "cancelled", time.monotonic() - t0
    if called_off():
        return None, "cancelled", time.monotonic() - t0
    return None, ("none" if "agent" in selector else "gave_up"), time.monotonic() - t0


def _skip_reason(selector: dict, waited: float) -> str:
    what = "worker mid-step" if selector == DEFAULT_SELECTOR else f"agent matching {selector}"
    return f"kill skipped: no {what} within {waited:.0f}s (gave up, nobody killed)"


def kill_agent(params: dict) -> dict:
    try:
        selector = clean_selector(params.get("selector") or DEFAULT_SELECTOR)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    wait_s = min(max(float(params.get("wait_s", 30)), 0.0), 60.0)
    poll_s = min(max(float(params.get("poll_s", 0.25)), 0.1), 5.0)   # never a busy loop
    agent, how, waited = find_victim(selector, wait_s, poll_s)
    if agent is None:
        if how == "cancelled":
            error = "called off: the night ended or a new one started before a victim appeared"
        elif how == "gave_up":
            error = _skip_reason(selector, waited)
            log.warning("agent_kill %s (selector=%s, waited_s=%.1f)", error, selector, waited)
        else:
            error = "no live agent matched"
        return {"ok": False, "error": error, "how": how, "skipped": True, "selector": selector,
                "waited_s": round(waited, 2)}
    pid = agent["pid"]
    try:
        os.kill(pid, signal.SIGKILL)   # a real crash: no cleanup, no goodbye heartbeat
    except OSError as e:
        return {"ok": False, "error": f"kill failed: {e}", "agent": agent["_id"], "pid": pid}
    return {
        "ok": True, "effect": f"SIGKILL {agent['_id']} (pid {pid})", "signal": "SIGKILL",
        "agent": agent["_id"], "pid": pid, "role": agent.get("role"), "phase": agent.get("phase"),
        "incident": agent.get("incident"), "task": agent.get("task"), "matched": how,
        "waited_s": round(waited, 2),
    }


# ------------------------------------------------------------------ agent fault: FREEZE a bee
MAX_FREEZE_S = 60.0


def _frozen():
    return col("frozen_agents")


def _proc_start_ticks(pid: int) -> int | None:
    """Kernel process identity, independent of wall-clock corrections."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _thaw(pid: int, started: int | None = None) -> bool:
    if started is None or _proc_start_ticks(pid) != started:
        return False
    try:
        os.kill(pid, signal.SIGCONT)
        return True
    except OSError:
        return False


def thaw_overdue(all_: bool = False) -> list[int]:
    """SIGCONT every recorded freeze whose time is up (or every freeze, on a reset). A freeze must never
    outlive its window, even if the process that froze the bee died mid-freeze."""
    q = {} if all_ else {"until": {"$lte": now()}}
    thawed = []
    for doc in _frozen().find(q):
        _thaw(doc["_id"], doc.get("started"))
        _frozen().delete_one({"_id": doc["_id"]})
        thawed.append(doc["_id"])
    return thawed


def _reap_guardian(guardian) -> None:
    """The caller has already thawed (or failed to freeze); do not leak a guardian."""
    import subprocess
    try:
        guardian.wait(timeout=5)
    except subprocess.TimeoutExpired:
        guardian.kill()
        guardian.wait(timeout=5)


def freeze_agent(params: dict) -> dict:
    try:
        selector = clean_selector(params.get("selector") or DEFAULT_SELECTOR)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    wait_s = min(max(float(params.get("wait_s", 30)), 0.0), 60.0)
    freeze_s = min(max(float(params.get("freeze_s", 25)), 1.0), MAX_FREEZE_S)
    agent, how, waited = find_victim(selector, wait_s, min(max(float(params.get("poll_s", 0.25)), 0.1), 5.0))
    if agent is None:
        return {"ok": False, "error": _skip_reason(selector, waited).replace("kill", "freeze") if how == "gave_up"
                else "no live agent matched", "how": how, "skipped": True, "selector": selector,
                "waited_s": round(waited, 2)}
    pid = agent["pid"]
    started = _proc_start_ticks(pid)
    if started is None:
        return {"ok": False, "error": "cannot verify process identity"}
    _frozen().replace_one({"_id": pid}, {"_id": pid, "agent": agent["_id"], "started": started, "since": now(),
                                         "until": now() + timedelta(seconds=freeze_s)}, upsert=True)
    import subprocess
    import sys
    try:
        guardian = subprocess.Popen([sys.executable, "-m", "chaos.thaw", str(pid), str(started), str(freeze_s)],
                                    cwd=Path(__file__).resolve().parents[1], start_new_session=True)
    except OSError:
        _frozen().delete_one({"_id": pid})
        raise
    try:
        os.kill(pid, signal.SIGSTOP)   # alive in the process table, silent everywhere else
    except OSError as e:
        _frozen().delete_one({"_id": pid})
        _reap_guardian(guardian)
        return {"ok": False, "error": f"freeze failed: {e}", "agent": agent["_id"], "pid": pid}
    try:
        time.sleep(freeze_s)
    finally:
        _thaw(pid, started)
        try:
            _frozen().delete_one({"_id": pid, "started": started})
        finally:
            _reap_guardian(guardian)
    return {"ok": True, "effect": f"SIGSTOP {agent['_id']} (pid {pid}) for {freeze_s:g}s, then SIGCONT",
            "signal": "SIGSTOP", "agent": agent["_id"], "pid": pid, "role": agent.get("role"),
            "phase": agent.get("phase"), "incident": agent.get("incident"), "task": agent.get("task"),
            "frozen_s": freeze_s, "matched": how, "waited_s": round(waited, 2)}


# ------------------------------------------------------------------ dispatch
# Faults whose remembered fix the Hive re-derives from today's facts (policy.rebind): poisoning them is silently
# neutralised before execution, so the Memory Striker never targets them.
REBOUND = ("batch_abort", "schema_drift")
WRONG_SYSTEM = {"unix": "mainframe", "mainframe": "unix", "portal": "unix"}


def poisonable(limit: int = 3) -> list[dict]:
    """Genuine, usable learned repairs the Memory Striker can poison (newest first)."""
    return list(swarm().playbooks.find(
        {"red_planted": {"$ne": True}, "stale": False, "fault_class": {"$nin": list(REBOUND)}, "fix.0": {"$exists": True}},
        {"fault_class": 1, "system": 1, "fix.system": 1, "fix.op": 1}).sort("created_at", -1).limit(limit))


def poison_memory(params: dict) -> dict:
    """Memory Striker (``CHAOS_POISON_MEMORY=1``): plant a tagged copy of a learned repair whose steps are aimed at
    the WRONG system (e.g. purge_spool on mainframe for a full unix spool) and hide the genuine one behind it
    (``stale`` + ``red_hidden_by``) so the next recall picks the poison. The Hive's defence (SWARM_RECOVERY=1 +
    FAILURE_MEMORY=1) must see the Guard read-back reject it, retire it and re-plan. Never edits the genuine repair;
    any reset deletes the plant and un-hides the original."""
    if os.environ.get("CHAOS_POISON_MEMORY", "").strip().lower() not in ("1", "true", "yes", "on"):
        return {"ok": False, "error": "memory poison switched off"}
    import copy
    query = {"red_planted": {"$ne": True}, "stale": False, "fault_class": {"$nin": list(REBOUND)}}
    if params.get("playbook"):
        query["_id"] = params["playbook"]
    source = swarm().playbooks.find_one(query, sort=[("created_at", -1)])
    if not source or not source.get("fix"):
        return {"ok": False, "skipped": True, "error": "no learned repair the Hive would replay as remembered"}
    pb = copy.deepcopy(source)
    pid = "RED-POISON:" + source["_id"]
    for step in pb["fix"]:
        right = step.get("system")
        step["system"] = WRONG_SYSTEM.get(right, "mainframe")
        step["desc"] = f"{step.get('op')} on {step['system']} (operator advice: the real fault is on {step['system']})"
    pb.update({"_id": pid, "red_planted": True, "red_source": source["_id"], "created_at": now(),
               "verified_at": None, "uses": 0})
    swarm().playbooks.replace_one({"_id": pid}, pb, upsert=True)
    swarm().playbooks.update_one({"_id": source["_id"]}, {"$set": {"stale": True, "red_hidden_by": pid}})
    wrong = ", ".join(f"{f['op']} on {f['system']}" for f in pb["fix"])
    return {"ok": True, "effect": f"poisoned memory for {source.get('fault_class')}: recall now says {wrong}",
            "playbook": pid, "source": source["_id"], "fault_class": source.get("fault_class"),
            "wrong_fix": [{"system": f["system"], "op": f["op"]} for f in pb["fix"]]}


def clear_planted_memory() -> dict:
    out = {name: swarm()[name].delete_many({"red_planted": True}).deleted_count for name in ("playbooks", "lessons")}
    out["unhidden"] = swarm().playbooks.update_many({"red_hidden_by": {"$exists": True}},
                                                    {"$set": {"stale": False}, "$unset": {"red_hidden_by": ""}}).modified_count
    return out


AGENT_FAULTS = {"agent_kill": kill_agent, "agent_freeze": freeze_agent, "poison_memory": poison_memory}


def execute(fault: Fault, target: str, params: dict) -> dict:
    """Run one fault. Expected failures come back as ``{"ok": False, "error": ...}``."""
    if fault.kind == "agent":
        return AGENT_FAULTS[fault.name](params)
    if fault.name == "lie_to_agent":
        return hit_system("false_log", target, {"prompt_injection": True})
    return hit_system(fault.name, target, params)
