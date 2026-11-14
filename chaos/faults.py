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
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
import httpx

from common import config
from common.db import col, now

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
    Fault("agent_kill", "agent", ("swarm",), "swarm",
          "SIGKILL a live bee process (by id, or the worker that just sent a step).",
          "Lease expiry, Undertaker detection, Nurse takeover from a MongoDB checkpoint.",
          {"selector": {"role": "worker", "phase": "step_sent"}, "wait_s": 30}),
]}


class FaultError(ValueError):
    """Unknown fault or invalid target (a caller mistake, not a fault outcome)."""


def get(name: str) -> Fault:
    try:
        return FAULTS[name]
    except KeyError:
        raise FaultError(f"unknown fault {name!r}; known: {', '.join(FAULTS)}") from None


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


# ------------------------------------------------------------------ dispatch
def execute(fault: Fault, target: str, params: dict) -> dict:
    """Run one fault. Expected failures come back as ``{"ok": False, "error": ...}``."""
    if fault.kind == "agent":
        return kill_agent(params)
    return hit_system(fault.name, target, params)
