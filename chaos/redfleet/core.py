"""Shared pieces of the Red fleet: settings, the Red Scout's read of the Hive, guardrails, LLM JSON."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from common import config
from common.db import now, swarm, world

ROLE = "red"
# Agents Red may never kill: the Gardener (a kill between deploy and proof can strand a half-done upgrade).
NEVER_KILL = {"gardener"}
UPGRADE_BUSY = ("planning", "testing", "awaiting_approval", "deploying", "deployed")


def enabled() -> bool:
    return os.environ.get("RED_FLEET", "").strip().lower() in ("1", "true", "yes", "on")


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    return default if raw is None else tuple(x.strip() for x in raw.split(",") if x.strip())


@dataclass
class Settings:
    """Pacing only. Default cap 2 strikes per agent per night so the estate can go green and autoplay
    continues (R1 finding 2); RED_FLEET_MAX_ATTACKS=0 = unlimited."""
    max_attacks: int = 2          # per agent per night; 0 = unlimited
    cooldown_s: float = 30.0      # per agent, between its own strikes (pacing, so the war room stays readable)
    first_strike_s: float = 0.0   # extra wait after a night starts (each persona adds its own stagger)
    poll_s: float = 2.0
    kill_wait_s: float = 6.0
    allowed: tuple[str, ...] = ()  # empty = every fault the persona knows
    victims: tuple[str, ...] = ()  # Assassin filter: "role" or "role:phase" patterns; empty = anyone killable
    skip_scenarios: tuple[str, ...] = ()
    use_llm: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        f = config.env_float
        return cls(
            max_attacks=int(f("RED_FLEET_MAX_ATTACKS", 2)),
            cooldown_s=max(0.0, f("RED_FLEET_COOLDOWN_S", 30)),
            first_strike_s=max(0.0, f("RED_FLEET_FIRST_STRIKE_S", 0)),
            poll_s=max(0.5, f("RED_FLEET_POLL_S", 2)),
            kill_wait_s=min(max(f("RED_FLEET_KILL_WAIT_S", 6), 0.0), 30.0),
            allowed=_csv("RED_FLEET_FAULTS", ()),
            victims=_csv("RED_FLEET_VICTIMS", ()),
            skip_scenarios=_csv("RED_FLEET_SKIP_SCENARIOS", ()),
            use_llm=os.environ.get("RED_FLEET_LLM", "1").strip().lower() not in ("0", "false", "no", "off"),
        )


def live_db_refusal() -> str | None:
    """The one hard rule: never attack the live demo databases unless explicitly allowed."""
    if os.environ.get("RED_FLEET_ALLOW_LIVE", "").strip() == "1":
        return None
    if config.WORLD_DB == "world" or config.SWARM_DB == "swarm":
        return ("refusing the live world/swarm DBs; use an isolated stack (WORLD_DB/SWARM_DB) "
                "or set RED_FLEET_ALLOW_LIVE=1")
    return None


# ------------------------------------------------------------------ Red Scout: read the Hive
def _fresh_cutoff() -> datetime:
    return now() - timedelta(seconds=max(2 * config.LEASE_S, 3 * config.HEARTBEAT_S))


def scout(agent_id: str) -> dict:
    """Everything an attacker needs, read from MongoDB. Red reads Blue's DB but never writes it
    (except thoughts, like every agent)."""
    from ..runner import refresh_outcomes
    recent_chaos = refresh_outcomes()
    s, w = swarm(), world()
    cur = s.stats.find_one({"_id": "current"}) or {}
    run_id = cur.get("run_id")
    run = w.runs.find_one({"_id": run_id}) if run_id else None
    incidents = []
    for inc in s.incidents.find({"active": True}).sort("opened_at", 1).limit(6):
        steps = inc.get("steps") or []
        incidents.append({
            "id": inc["_id"], "kind": inc.get("kind", "chain"), "status": inc.get("status"),
            "system": inc.get("system"), "root_cause_system": inc.get("root_cause_system"),
            "fault_class": inc.get("fault_class"), "affected": inc.get("affected") or [],
            "pending_steps": [{"op": st.get("op"), "system": st.get("system"), "status": st.get("status")}
                              for st in steps if st.get("status") in ("planned", "sent")][:4],
        })
    agents = [{"id": a["_id"], "role": a.get("role"), "status": a.get("status"), "phase": a.get("phase"),
               "specialty": a.get("specialty"), "incident": a.get("incident")}
              for a in s.agents.find({"status": {"$ne": "dead"}, "last_seen": {"$gte": _fresh_cutoff()}},
                                     {"role": 1, "status": 1, "phase": 1, "specialty": 1, "incident": 1}).sort("_id", 1)]
    learned = [{"fault_class": p.get("fault_class"), "system": p.get("system")}
               for p in s.playbooks.find({}, {"fault_class": 1, "system": 1}).sort("created_at", -1).limit(3)]
    systems = {d["_id"]: {"status": d.get("status"), "version": d.get("version"),
                          "faults_active": [f.get("fault") for f in d.get("faults_active") or []],
                          "open_vulns": [v["id"] for v in d.get("vulns") or [] if v.get("open")]}
               for d in w.systems.find({}, {"status": 1, "version": 1, "faults_active": 1, "vulns": 1})}
    feed = (w.system_state.find_one({"_id": "unix:feed"}, {"data.field": 1}) or {}).get("data") or {}
    if "unix" in systems and feed.get("field"):
        systems["unix"]["feed_field"] = feed["field"]   # what the Drifter renames next
    mine, fleet = [], []
    if run_id:
        for e in w.chaos_events.find({"run_id": run_id, "by": {"$regex": "^red-"}},
                                     {"fault": 1, "target": 1, "status": 1, "ts": 1, "by": 1, "outcome": 1, "lesson": 1}).sort("ts", 1):
            row = {"fault": e["fault"], "target": e.get("target"), "status": e.get("status"), "ts": e.get("ts"),
                   "by": e.get("by"), "outcome": e.get("outcome"), "lesson": e.get("lesson")}
            fleet.append(row)
            if e.get("by") == agent_id:
                mine.append(row)
    from .learning import outcome
    lessons = (w.red_agents.find_one({"_id": agent_id}) or {}).get("lessons", [])
    for lesson in lessons:
        event = w.chaos_events.find_one({"_id": lesson.get("event"), "ts": {"$lte": lesson.get("ts", now())}})
        if event:
            lesson["outcome"] = outcome(event)  # late recovery is visible at the next THINK
    return {
        "run_id": run_id, "scenario": (run or {}).get("scenario") or cur.get("scenario"),
        "night_running": bool(run and run.get("ended_at") is None),
        "night_started": (run or {}).get("started_at"),
        "incidents": incidents, "agents": agents, "systems": systems,
        "mid_send": [a for a in agents if a["role"] == "worker" and a["status"] == "working"
                     and a["phase"] == "step_sent"],
        "approvals_pending": s.approvals.count_documents({"status": "pending"}),
        "upgrade_busy": s.upgrade_requests.count_documents({"status": {"$in": list(UPGRADE_BUSY)}}),
        "learned": learned, "red_attacks": mine, "fleet_attacks": fleet,
        "lessons": lessons,
        "attack_history": [{k: e.get(k) for k in ("fault", "target", "who", "why", "outcome", "lesson")}
                           for e in recent_chaos[:6]],
        "red_board": list(w.red_agents.find({}, {"name": 1, "status": 1, "intent": 1, "last_reflection": 1, "last_seen": 1})),
    }


def _age_s(ts: datetime | None, at: datetime) -> float:
    return float("inf") if ts is None else (at - ts).total_seconds()


def gate(snap: dict, cfg: Settings, stagger_s: float = 0.0, at: datetime | None = None) -> str | None:
    """Why this attacker must hold its fire right now, or None. Only pacing and no-permanent-damage rules."""
    at = at or now()
    if not snap.get("night_running"):
        return "no night running"
    if snap.get("scenario") in cfg.skip_scenarios:
        return f"{snap['scenario']} is off limits"
    if snap.get("upgrade_busy"):
        return "the Gardener is mid-upgrade"   # a strike mid-deploy could strand a half-done upgrade
    mine = snap.get("red_attacks") or []
    if cfg.max_attacks and len(mine) >= cfg.max_attacks:
        return f"attack budget spent ({len(mine)}/{cfg.max_attacks})"
    if any(a.get("status") == "firing" for a in mine):
        return "my last attack is still in flight"
    if _age_s(snap.get("night_started"), at) < cfg.first_strike_s + stagger_s:
        return "waiting for the night to unfold"
    last = max((a["ts"] for a in mine if a.get("ts")), default=None)
    if _age_s(last, at) < cfg.cooldown_s:
        return "cooling down"
    return None


# ------------------------------------------------------------------ LLM JSON
def parse_json(text: str | None) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def brief(snap: dict) -> str:
    """The state, compact, for the planner prompt (no timestamps)."""
    def clean(rows):
        return [{k: v for k, v in r.items() if k != "ts"} for r in rows or []]
    return json.dumps({
        "recent_seeded_manual_and_fleet_outcomes": snap.get("attack_history", []),
        "my_recent_outcomes_and_lessons": snap.get("lessons", [])[-6:],
        "shared_red_board": snap.get("red_board", []),
        "scenario": snap.get("scenario"), "open_incidents": snap.get("incidents"),
        "live_agents": [{k: a[k] for k in ("id", "role", "status", "phase")} for a in snap.get("agents") or []],
        "systems": snap.get("systems"), "recently_learned": snap.get("learned"),
        "fleet_attacks_this_night": clean(snap.get("fleet_attacks"))[-8:],
    }, default=str)
