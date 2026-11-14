"""Runner decisions (``RUNNER_DECIDES=1``, default off). With the switch off nothing here runs and
``runner.fire`` behaves exactly as before.

On, the Runner stops being a dumb door and decides:

* **admit**: every Red fire (Strikers, Commander orders, Planner requests, the Runner's own picks) passes the
  stage caps first: ``execute`` | ``hold`` (nothing fired, try later) | ``deny`` (budget spent / off limits).
* **pick**: a loop (a thread in the ``python -m chaos.redfleet`` supervisor) chooses ONE candidate per tick among
  queued Planner/Commander requests (``source=planner``), a memory poison of what the Hive just learned
  (``source=poison``) and a replay of a seeded attack the Hive has already met (``source=seeded``).
* **timing + route**: when to strike, and whether to ``spawn`` the Striker that owns the fault (agent-victim
  faults need a Striker's victim choice at strike time) or ``execute`` it directly.
* Seeded scenario steps are always executed on schedule (logged, never held: they are the demo contract).

Every decision is one ``world.chaos_events`` doc in W34's shared decision log (``chaos.decisions``:
``{kind:"decision", by:"runner", decision:<verdict>, origin:<source>, choice, reason, caps, event}``; fault readers
skip ``kind:"decision"``) plus a feed line from ``runner``.
Rule-based by default; ``RUNNER_LLM=1`` lets Claude pick among the vetted candidates (never invent one).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from pymongo import ReturnDocument

from common import config
from common.db import next_id, now, swarm, world
from common.events import think

from . import decisions, faults

log = logging.getLogger("chaos.decider")

RUNNER_ID = "red-runner"     # the Runner signs its own strikes as a red-* attacker (counts toward the caps)
BOARD_ID = "runner"           # its row on world.red_agents: reservations, requests, spawns
AGENT_VICTIM = ("agent_kill", "agent_freeze")
SOURCE_RANK = {"planner": 3, "poison": 2, "seeded": 1}
UPGRADE_BUSY = ("planning", "testing", "awaiting_approval", "deploying", "deployed")


def _on(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    return _on("RUNNER_DECIDES")


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.environ.get(name, default).split(",") if x.strip())


@dataclass
class Caps:
    max_per_night: int = 3        # Red strikes per night, every red source together (0 = unlimited)
    gap_s: float = 20.0           # between two Red strikes
    first_s: float = 10.0         # the Runner's own picks wait this long into a night
    tick_s: float = 3.0
    max_spawns: int = 1           # Striker processes the Runner may start per night
    quiet: tuple[str, ...] = ("bank-night-3",)                    # no Red at all (R1 finding 4)
    protected: tuple[str, ...] = ("bank-night-1", "bank-night-2")  # agent attacks only, no replays (round-2 timing)

    @classmethod
    def from_env(cls) -> "Caps":
        f = config.env_float
        return cls(max_per_night=int(f("RUNNER_MAX_PER_NIGHT", 3)), gap_s=max(0.0, f("RUNNER_GAP_S", 20)),
                   first_s=max(0.0, f("RUNNER_FIRST_S", 10)), tick_s=max(0.2, f("RUNNER_TICK_S", 3)),
                   max_spawns=int(f("RUNNER_MAX_SPAWNS", 1)),
                   quiet=_csv("RUNNER_QUIET_SCENARIOS", "bank-night-3"),
                   protected=_csv("RUNNER_PROTECTED_SCENARIOS", "bank-night-1,bank-night-2"))


# ------------------------------------------------------------------ state (cheap reads, no model)
def state() -> dict:
    s, w = swarm(), world()
    cur = s.stats.find_one({"_id": "current"}) or {}
    run = w.runs.find_one({"_id": cur["run_id"]}) if cur.get("run_id") else None
    if run is None:
        from .runner import current_run
        run = current_run()
    started = (run or {}).get("started_at")
    q = {"run_id": (run or {}).get("_id"), "by": {"$regex": "^red-"}, **decisions.NOT_DECISION}
    if started:
        q["ts"] = {"$gte": started}
    red = list(w.chaos_events.find(q, {"ts": 1, "fault": 1, "by": 1}).sort("ts", -1)) if run else []
    return {
        "run_id": (run or {}).get("_id"), "round": (run or {}).get("round"), "scenario": (run or {}).get("scenario"),
        "night_running": bool(run and run.get("ended_at") is None), "night_started": started,
        "night_key": f"{(run or {}).get('_id')}@{started.isoformat() if started else '-'}",
        "run_status": (run or {}).get("status"),
        "open_incidents": s.incidents.count_documents({"active": True}),
        "upgrade_busy": s.upgrade_requests.count_documents({"status": {"$in": list(UPGRADE_BUSY)}}),
        "systems_up": all(d.get("status") == "up" for d in w.systems.find({}, {"status": 1})),
        "red_tonight": len(red), "last_red": red[0]["ts"] if red else None,
    }


def _age(ts: datetime | None) -> float:
    return float("inf") if ts is None else (now() - ts).total_seconds()


def _caps_view(st: dict, caps: Caps) -> dict:
    return {"night_red": st["red_tonight"], "max_per_night": caps.max_per_night, "gap_s": caps.gap_s,
            "since_last_s": None if st["last_red"] is None else round(_age(st["last_red"]), 1),
            "open_incidents": st["open_incidents"]}


# ------------------------------------------------------------------ the decision log
_said: dict[str, float] = {}


def record(verdict: str, source: str, reason: str, choice: dict | None = None, st: dict | None = None,
           caps: Caps | None = None, **more) -> dict:
    """One decision -> one shared decision-log doc (W34's ``chaos.decisions``: ``kind:"decision"``, ``by:"runner"``,
    ``decision:<verdict>``, ``origin:<source>``) + a rate-limited feed line. Returns the doc."""
    st = st or state()
    caps = caps or Caps.from_env()
    choice = {k: v for k, v in (choice or {}).items() if k in ("fault", "target", "by", "request", "params")}
    doc = decisions.log_decision(BOARD_ID, verdict, run_id=st["run_id"], reason=reason, verdict=verdict,
                                 origin=source, choice=choice, caps=_caps_view(st, caps), event=None, **more)
    what = f"{choice.get('fault')} on {choice.get('target')}" if choice.get("fault") else "nothing"
    route = f" via {more['route']}" if more.get("route") else ""
    key = f"{verdict}|{source}|{choice.get('fault')}|{choice.get('by')}|{reason}"
    if time.monotonic() - _said.get(key, -1e9) >= 20:   # a held Striker retries every tick: say it once per 20 s
        _said[key] = time.monotonic()
        think("runner", "red", f"Runner decides ({doc['_id']}): {verdict} {what}{route} [{source}"
                               f"{', from ' + choice['by'] if choice.get('by') else ''}]: {reason}",
              system=choice.get("target") if choice.get("target") not in (None, "swarm") else None,
              kind="action" if verdict in ("execute", "spawn") else "thought")
    return doc


def link(decision_id: str, event_id: str) -> None:
    world().chaos_events.update_one({"_id": decision_id}, {"$set": {"event": event_id}})


# ------------------------------------------------------------------ admit (every red fire)
def _victim_reason(fault: str, params: dict) -> str | None:
    sel = (params or {}).get("selector") or {}
    if sel.get("agent"):
        a = swarm().agents.find_one({"_id": sel["agent"]}, {"role": 1}) or {}
        if a.get("role") != "worker":
            return f"{sel['agent']} is a {a.get('role') or 'unknown'}: agent faults only hit workers on stage"
    elif sel.get("role") not in (None, "worker"):
        return f"agent faults only hit workers on stage, not {sel.get('role')}"
    return None


def _ensure_night(st: dict) -> None:
    """The Runner's board row, with its per-night counters reset when a new night (or a full reset) starts."""
    b = world().red_agents
    b.update_one({"_id": BOARD_ID}, {"$setOnInsert": {"requests": []}}, upsert=True)
    b.update_one({"_id": BOARD_ID, "night": {"$ne": st["night_key"]}},
                 {"$set": {"night": st["night_key"], "strikes": 0, "spawns": 0, "last_strike_at": datetime(2000, 1, 1)}})


def _reserve(st: dict, caps: Caps, gap_s: float | None = None) -> bool:
    """Atomically take one strike slot for this night (budget + gap), so concurrent Strikers can't both pass."""
    b = world().red_agents
    _ensure_night(st)
    q = {"_id": BOARD_ID, "night": st["night_key"], "last_strike_at": {"$lte": now() - timedelta(seconds=caps.gap_s if gap_s is None else gap_s)}}
    if caps.max_per_night:
        q["strikes"] = {"$lt": caps.max_per_night}
    return b.find_one_and_update(q, {"$inc": {"strikes": 1}, "$set": {"last_strike_at": now()}},
                                 return_document=ReturnDocument.AFTER) is not None


def _rules(fault: str, params: dict, st: dict, caps: Caps, campaign: bool = False) -> tuple[str, str] | None:
    """(verdict, reason) when a rule stops the strike, else None. Order = most permanent first."""
    if not st["night_running"]:
        return "hold", "no night running"
    if st["scenario"] in caps.quiet:
        return "deny", f"{st['scenario']} is a quiet night for Red (the upgrade proof must stay clean)"
    if caps.max_per_night and st["red_tonight"] >= caps.max_per_night:
        return "deny", f"night budget spent ({st['red_tonight']}/{caps.max_per_night}): the estate must go green so autoplay can move on"
    f = faults.FAULTS.get(fault)
    if f is not None and f.kind == "system" and st["scenario"] in caps.protected:
        return "deny", (f"{st['scenario']} is a demo night: Red hits the agents, not the estate (an extra incident "
                        "would skew the round-2 timing)")
    if fault in AGENT_VICTIM:
        why = _victim_reason(fault, params)
        if why:
            return "deny", why
    if st["upgrade_busy"]:
        return "hold", "the Gardener is mid-upgrade"
    if not campaign and _age(st["last_red"]) < caps.gap_s:   # a Commander campaign keeps its own step gap
        return "hold", f"spacing strikes: last Red strike {_age(st['last_red']):.0f}s ago (< {caps.gap_s:g}s)"
    if f is not None and f.kind == "system" and st["open_incidents"]:
        return "hold", (f"{st['open_incidents']} incident(s) still open: a second system fault mid-repair strands "
                        "the Hive (R1 #3), wait for green")
    if (_on("DEMO_AUTOPLAY") and st["run_status"] in ("played", "cancelled") and not st["open_incidents"]
            and st["systems_up"] and st["red_tonight"]):
        return "hold", "the estate is green after tonight's attacks: autoplay is counting its green gap"
    return None


def admit(fault: str, target: str | None, params: dict | None, by: str, source: str = "striker",
          reserve: bool = True, st: dict | None = None, caps: Caps | None = None, **more) -> dict:
    """Decide on one Red fire. Returns the decision doc (``verdict`` execute | hold | deny)."""
    st, caps = st or state(), caps or Caps.from_env()
    choice = {"fault": fault, "target": target, "by": by, **({"request": more.pop("request")} if "request" in more else {})}
    campaign = source == "commander"
    stop = _rules(fault, params or {}, st, caps, campaign)
    if stop is None and reserve and not _reserve(st, caps, 0.0 if campaign else None):
        stop = ("hold", "another Red strike just took the slot (budget/spacing)")
    if stop:
        return record(stop[0], source, stop[1], choice, st, caps, **more)
    return record("execute", source, more.pop("why", None) or f"caps clear ({st['red_tonight']}/"
                  f"{caps.max_per_night or '∞'} tonight, {st['open_incidents']} open incidents)", choice, st, caps, **more)


def refused(decision: dict, fault: str, target: str | None, params: dict | None) -> dict:
    """What runner.fire returns for a held/denied strike: looks like a missed event, no CHX was written."""
    return {"_id": decision["_id"], "fault": fault, "target": target, "params": params or {},
            "status": decision["decision"] if decision["decision"] != "execute" else "hold", "ts": decision["ts"],
            "result": {"ok": False, "error": f"Runner {decision['decision']}: {decision['reason']}"},
            "runner_decision": decision["_id"]}


def seeded_step(step: dict, scenario: str) -> dict:
    """Seeded scenario steps are the demo contract: always executed on schedule, but the decision is logged."""
    return record("execute", "seeded", f"scheduled step t={step.get('t')} of {scenario}: seeded drills are never held",
                  {"fault": step["fault"], "target": step.get("target"), "by": f"scenario:{scenario}"})


# ------------------------------------------------------------------ requests (Planner / Commander -> Runner)
def propose(fault: str, target: str | None = None, params: dict | None = None, by: str = "red-planner",
            why: str = "", campaign: dict | None = None, prefer: str = "auto") -> str:
    """Queue a request for the Runner loop; non-blocking. Returns the request id (RRQ-xxxx)."""
    faults.get(fault)   # unknown fault -> FaultError now, not later
    rid = next_id("RRQ", db=world())
    world().red_agents.update_one({"_id": BOARD_ID}, {"$push": {"requests": {
        "id": rid, "fault": fault, "target": target, "params": params or {}, "by": by, "why": why[:300],
        "campaign": campaign, "prefer": prefer if prefer in ("auto", "execute", "spawn") else "auto",
        "ts": now(), "status": "queued"}}}, upsert=True)
    return rid


def _set_request(rid: str, status: str, decision: str) -> None:
    world().red_agents.update_one({"_id": BOARD_ID, "requests.id": rid},
                                  {"$set": {"requests.$.status": status, "requests.$.decision": decision}})


# ------------------------------------------------------------------ candidates + pick
def _owner(fault: str) -> str | None:
    from .redfleet.personas import PERSONAS
    return next((p.id for p in PERSONAS.values() if fault in p.faults), None)


def candidates(st: dict, caps: Caps) -> list[dict]:
    out: list[dict] = []
    started = st["night_started"]
    board = world().red_agents.find_one({"_id": BOARD_ID}, {"requests": 1}) or {}
    for r in board.get("requests") or []:
        if r.get("status") in ("queued", "held") and (not started or r.get("ts") is None or r["ts"] >= started):
            out.append({"source": "planner", "fault": r["fault"], "target": r.get("target"), "params": r.get("params") or {},
                        "by": r.get("by"), "request": r["id"], "prefer": r.get("prefer", "auto"),
                        "why": r.get("why") or f"requested by {r.get('by')}"})
    pm = faults.FAULTS.get("poison_memory")
    if pm is not None and pm.enabled():
        s = swarm()
        learned = s.playbooks.find_one({"red_planted": {"$ne": True}, "stale": False,
                                        "fault_class": {"$nin": list(getattr(faults, "REBOUND", ()))}},
                                       sort=[("created_at", -1)])
        if learned and learned.get("fix") and not s.playbooks.find_one({"red_planted": True}, {"_id": 1}):
            out.append({"source": "poison", "fault": "poison_memory", "target": "swarm", "params": {},
                        "by": RUNNER_ID, "prefer": "auto",
                        "why": f"the Hive just learned {learned['_id']} ({learned.get('fault_class')}): poison that "
                               "memory and see if the Guard's read-back rejects it"})
    if st["scenario"] and st["scenario"] not in caps.protected and st["scenario"] not in caps.quiet:
        from . import scenarios
        try:
            steps = scenarios.get(st["scenario"]).get("steps") or []
        except Exception:
            steps = []
        mine = world().chaos_events.count_documents({"run_id": st["run_id"], "by": RUNNER_ID, "red.planner": "seeded"})
        for step in steps[:1] if not mine else []:
            out.append({"source": "seeded", "fault": step["fault"], "target": step.get("target"),
                        "params": step.get("params") or {}, "by": RUNNER_ID, "prefer": "auto",
                        "why": f"replay tonight's seeded {step['fault']} on {step.get('target')}: does recall make it faster?"})
    return out


def _llm_pick(cands: list[dict], st: dict) -> tuple[int | None, str]:
    """Optional (RUNNER_LLM=1 + a key): Claude picks an index among the vetted candidates. Never a new attack."""
    from common import llm
    if not (_on("RUNNER_LLM") and llm.llm_available()):
        return None, ""
    menu = [{"i": i, "source": c["source"], "fault": c["fault"], "target": c["target"], "why": c["why"]}
            for i, c in enumerate(cands)]
    raw = llm.complete("You are the Red Runner. Pick ONE item index from the menu that best tests the Hive now. "
                       'Answer JSON only: {"i": <int>, "reason": "<one sentence>"}',
                       json.dumps({"menu": menu, "open_incidents": st["open_incidents"], "scenario": st["scenario"]}),
                       max_tokens=150)
    from .redfleet.core import parse_json
    out = parse_json(raw) or {}
    i = out.get("i")
    if isinstance(i, int) and 0 <= i < len(cands):
        return i, str(out.get("reason") or "")[:200]
    return None, "Claude's pick was off the menu"


def pick(cands: list[dict], st: dict) -> tuple[dict, str, str]:
    """(candidate, how, reason). Rule: Planner requests first (oldest), then poison, then seeded replays."""
    i, why = _llm_pick(cands, st)
    if i is not None:
        return cands[i], "claude", why
    best = max(cands, key=lambda c: SOURCE_RANK.get(c["source"], 0))
    others = ", ".join(sorted({c["source"] for c in cands} - {best["source"]})) or "none"
    return best, "rule", f"{best['source']} outranks {others}: {best['why']}"


def route(c: dict, spawns_tonight: int, caps: Caps, online: set[str]) -> tuple[str, str, str | None]:
    """(route, reason, persona). spawn = start the owning Striker (it picks the live victim and fires through the
    Runner); execute = the Runner fires the vetted fault itself."""
    owner = _owner(c["fault"])
    wants_spawn = c.get("prefer") == "spawn" or (c.get("prefer") == "auto" and c["fault"] in AGENT_VICTIM)
    if not wants_spawn or owner is None:
        return "execute", "a fixed vetted fault: no live choice needed, the Runner fires it directly", owner
    if owner in online:
        return "execute", f"{owner} is already online: no second Striker, the Runner fires it directly", owner
    if caps.max_spawns and spawns_tonight >= caps.max_spawns:
        return "execute", f"spawn budget spent ({spawns_tonight}/{caps.max_spawns}): the Runner fires it directly", owner
    return "spawn", f"{c['fault']} needs a live victim choice at strike time: spawn {owner} for one strike", owner


# ------------------------------------------------------------------ the loop
class RunnerLoop:
    def __init__(self, caps: Caps | None = None):
        self.caps = caps or Caps.from_env()
        self._stop = threading.Event()
        self.spawned: dict[str, subprocess.Popen] = {}
        self._night = None

    def _online(self) -> set[str]:
        cut = now() - timedelta(seconds=30)
        return {d["_id"] for d in world().red_agents.find({"last_seen": {"$gte": cut}}, {"_id": 1})}

    def _reap(self, night_key: str) -> None:
        if self._night != night_key:   # a new night: retire one-shot Strikers from the last one
            for p in self.spawned.values():
                if p.poll() is None:
                    p.terminate()
            self.spawned.clear()
            self._night = night_key

    def spawn(self, persona: str) -> subprocess.Popen:
        env = {**os.environ, "RED_FLEET_MAX_ATTACKS": "1", "RED_FLEET_FIRST_STRIKE_S": "0"}
        p = subprocess.Popen([sys.executable, "-m", "chaos.redfleet", "--agent", persona], env=env)
        self.spawned[persona] = p
        world().red_agents.update_one({"_id": BOARD_ID}, {"$inc": {"spawns": 1}})
        return p

    def tick(self) -> dict | None:
        """One decision round. Returns the final decision doc, or None when there was nothing to decide."""
        st = state()
        if not st["night_running"]:
            return None
        self._reap(st["night_key"])
        _ensure_night(st)
        if _age(st["night_started"]) < self.caps.first_s:
            return None
        cands = candidates(st, self.caps)
        if not cands:
            return None
        c, how, why = pick(cands, st)
        record("pick", c["source"], f"({how}) {why}", c, st, self.caps,
               candidates=[{k: x.get(k) for k in ("source", "fault", "target", "by", "request")} for x in cands])
        board = world().red_agents.find_one({"_id": BOARD_ID}, {"spawns": 1, "night": 1}) or {}
        spawns = board.get("spawns", 0) if board.get("night") == st["night_key"] else 0
        rt, rwhy, persona = route(c, spawns, self.caps, self._online())
        extra = {"request": c["request"]} if c.get("request") else {}
        if rt == "spawn":
            stop = _rules(c["fault"], c["params"], st, self.caps)
            if stop:
                d = record(stop[0], c["source"], stop[1], {**c, **extra}, st, self.caps)
            else:
                self.spawn(persona)
                d = record("spawn", c["source"], rwhy, {**c, **extra}, st, self.caps, route="spawn", persona=persona)
        else:
            d = admit(c["fault"], c["target"], c["params"], c["by"], source=c["source"], st=st, caps=self.caps,
                      route="execute", why=rwhy, **extra)
            if d["decision"] == "execute":
                threading.Thread(target=self._fire, args=(c, d, st), daemon=True, name="runner-fire").start()
        if c.get("request"):
            _set_request(c["request"], {"execute": "executed", "spawn": "spawned"}.get(d["decision"], d["decision"]
                                                                                       if d["decision"] != "hold" else "held"), d["_id"])
        return d

    def _fire(self, c: dict, d: dict, st: dict) -> None:
        from . import runner
        try:
            runner.fire(c["fault"], c["target"], c["params"], manual=False, run_id=st["run_id"],
                        who=RUNNER_ID, why=c["why"],
                        extra={"by": RUNNER_ID, "runner_decision": d["_id"],
                               "red": {"persona": "Runner", "planner": c["source"], "reason": c["why"],
                                       "requested_by": c.get("by"), "request": c.get("request")}})
        except Exception:
            log.exception("runner strike failed")

    def run(self) -> None:
        think("runner", "red", "Runner online (RUNNER_DECIDES): picks among Planner requests, memory poison and "
                               "seeded replays; spaces strikes and keeps the night's budget so autoplay never stalls.")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("runner tick failed")
            self._stop.wait(self.caps.tick_s)
        for p in self.spawned.values():
            if p.poll() is None:
                p.terminate()

    def stop(self, *_):
        self._stop.set()


def start_thread() -> RunnerLoop:
    loop = RunnerLoop()
    threading.Thread(target=loop.run, daemon=True, name="red-runner").start()
    return loop
