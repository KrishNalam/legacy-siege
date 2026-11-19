"""Comms live roster (SWARM_COMMS=1): who every agent is, what it is doing and where, in agents.roster.

Derived, not reported: one thread inside the Waggle process reads the state the agents already write
(swarm.agents heartbeat/status/phase, the task and incident LEASES, incident steps + handoffs,
spawn_requests) and upserts one doc per agent, writing only docs that changed. No other role writes it.

    agents.roster {_id: agent id, role, specialty, state: "idle"|"working"|"standby"|"dead"|"retired",
                  incident, system, task, step, phase, doing: one line, parent (current orchestrator: the
                  incident's lease holder for task holders, the assigning queen for scouts), spawned_by,
                  took_over: {from, work, ts, takeover_s} | null, taken_over_by: {to, work, ts} | null,
                  pid, generation, last_seen, died_at, retired_at, updated_at}
"""
from __future__ import annotations

import threading
from datetime import timedelta

from pymongo import DeleteOne, UpdateOne

from common import config
from common.db import col, now

SWEEP_S = config.env_float("ROSTER_SWEEP_S", 1.0)
RETIRED_KEEP_S = 60       # a retired (scaled-down) bee stays listed this long
RECENT_S = 300            # incidents closed this recently still explain takeovers
FULL_REFRESH_EVERY = 10   # every Nth sweep rewrites every doc (self-heals after a full reset wipe)
SYSTEMS = ("portal", "unix", "mainframe")


def _step_text(st: dict) -> str:
    return f"{st.get('step_id')} {st.get('op')} ({st.get('status')})"


def build(agents: list[dict], tasks: list[dict], incidents: list[dict], spawns: list[dict]) -> dict[str, dict]:
    """Pure: current state of the hive -> {agent_id: roster doc (without timestamps)}."""
    incs = {i["_id"]: i for i in incidents}
    task_of = {t["owner"]: t for t in tasks if t.get("owner")}
    inc_of = {i["owner"]: i for i in incidents if i.get("owner") and i.get("active")}
    spawned_by = {s["agent_id"]: s.get("requested_by") for s in spawns if s.get("agent_id")}
    took, taken = {}, {}
    for i in sorted(incidents, key=lambda i: str(i.get("opened_at") or "")):
        for h in i.get("handoffs") or []:
            if h.get("to"):
                took[h["to"]] = {"from": h.get("from"), "work": h.get("work") or i["_id"], "ts": h.get("ts"),
                                 "takeover_s": h.get("takeover_s")}
            if h.get("from"):
                taken[h["from"]] = {"to": h.get("to"), "work": h.get("work") or i["_id"], "ts": h.get("ts")}
    out = {}
    for a in agents:
        aid, role = a["_id"], a.get("role")
        state = a.get("status") or "idle"
        target = a.get("target")
        task, inc, step, parent = task_of.get(aid), inc_of.get(aid), None, None
        inc_id = a.get("incident") or (task or {}).get("incident") or (inc or {}).get("_id")
        if task:
            i = incs.get(task.get("incident")) or {}
            parent = i.get("owner") or i.get("assigned_to")
            mine = [s for s in i.get("steps") or [] if s.get("step_id") in (task.get("step_ids") or [])]
            cur = next((s for s in mine if s.get("status") != "verified"), mine[-1] if mine else None)
            step = _step_text(cur) if cur else f"diagnose {task.get('specialty')}"
        elif inc:
            parent = inc.get("assigned_by") or ("queen" if role == "scout" else None)
            cur = next((s for s in inc.get("steps") or [] if s.get("status") not in ("verified",)), None)
            step = _step_text(cur) if cur else inc.get("status")
        elif inc_id and inc_id in incs:
            parent = incs[inc_id].get("owner")
        system = target if target in SYSTEMS else (incs.get(inc_id) or {}).get("system") if inc_id else None
        if state == "dead":
            t = taken.get(aid)
            doing = f"dead — {t['work']} taken over by {t['to']}" if t else "dead — stopped heartbeating"
        elif state == "standby":
            doing = f"standby on the bench (for {a['standby_for']})" if a.get("standby_for") else "standby on the bench"
        elif a.get("phase") == "retiring":
            doing = "retiring"
        elif inc_id or task:
            what = a.get("phase") or "working"
            doing = f"{what} {(task or {}).get('_id') or inc_id}" + (f" on {system}" if system else "")
            if step:
                doing += f": {step}"
            if aid in took and (took[aid]["work"] in (inc_id, (task or {}).get("_id"))):
                doing += f" (took over from {took[aid]['from']})"
        else:
            doing = "idle in the hive"
        out[aid] = {
            "role": role, "specialty": a.get("specialty"), "state": state, "incident": inc_id, "system": system,
            "task": (task or {}).get("_id") or a.get("task"), "step": step, "phase": a.get("phase"),
            "doing": doing[:200], "parent": parent, "spawned_by": spawned_by.get(aid),
            "took_over": took.get(aid), "taken_over_by": taken.get(aid), "pid": a.get("pid"),
            "generation": a.get("generation"), "died_at": a.get("died_at"), "retired_at": None,
        }
    return out


class Roster:
    """Holds what it last wrote, so a sweep writes only changed docs (+ heartbeat age refresh)."""

    def __init__(self) -> None:
        self.written: dict[str, dict] = {}
        self.hb_written: dict[str, object] = {}
        self.n = 0

    def sweep(self) -> int:
        self.n += 1
        # Every Nth sweep force-writes every live doc (self-heals a roster wiped behind our back) without
        # forgetting the lifecycle cache: clearing self.written would drop retired agents before their
        # RETIRED_KEEP_S expiry and leave their rows in agents.roster forever.
        force = self.n % FULL_REFRESH_EVERY == 0
        t = now()
        agents = list(col("agents").find({}, {"role": 1, "specialty": 1, "status": 1, "target": 1, "incident": 1,
                                             "task": 1, "phase": 1, "pid": 1, "last_seen": 1, "standby_for": 1,
                                             "generation": 1, "died_at": 1}))
        tasks = list(col("tasks").find({"status": "claimed", "owner": {"$ne": None}},
                                       {"incident": 1, "step_ids": 1, "specialty": 1, "owner": 1}))
        incidents = list(col("incidents").find(
            {"$or": [{"active": True}, {"closed_at": {"$gte": t - timedelta(seconds=RECENT_S)}}]},
            {"owner": 1, "assigned_to": 1, "assigned_by": 1, "system": 1, "status": 1, "active": 1,
             "opened_at": 1, "handoffs": 1, "steps.step_id": 1, "steps.op": 1, "steps.status": 1}))
        spawns = list(col("spawn_requests").find({"status": "spawned"}, {"agent_id": 1, "requested_by": 1}))
        if not agents:  # a full reset wiped swarm.agents: the roster starts over too
            if self.written:
                col("roster").delete_many({})
                self.written.clear()
                self.hb_written.clear()
            return 0
        docs = build(agents, tasks, incidents, spawns)
        seen = {a["_id"]: a.get("last_seen") for a in agents}
        ops = []
        for aid, d in docs.items():
            hb, prev_hb = seen.get(aid), self.hb_written.get(aid)
            hb_due = hb is not None and (prev_hb is None or (hb - prev_hb).total_seconds() >= config.HEARTBEAT_S)
            if force or d != self.written.get(aid) or hb_due:
                ops.append(UpdateOne({"_id": aid}, {"$set": {**d, "last_seen": hb, "updated_at": t}}, upsert=True))
                self.written[aid], self.hb_written[aid] = d, hb
        for aid in list(self.written):
            if aid in docs:
                continue
            prev = self.written[aid]
            if prev.get("state") != "retired":  # agent doc deleted: retired (scaled down) or wiped by a reset
                d = {**prev, "state": "retired", "doing": "retired (swarm scaled down)", "retired_at": t,
                     "incident": None, "task": None, "step": None, "phase": None}
                ops.append(UpdateOne({"_id": aid}, {"$set": {**d, "updated_at": t}}))
                self.written[aid] = d
            elif (t - prev["retired_at"]).total_seconds() > RETIRED_KEEP_S:
                ops.append(DeleteOne({"_id": aid}))
                self.written.pop(aid, None)
                self.hb_written.pop(aid, None)
        if ops:
            col("roster").bulk_write(ops, ordered=False)
        return len(ops)


def start(agent) -> threading.Thread:
    """Run the roster sweep beside the Waggle's message loop (its own thread: Atlas round trips here
    never delay message prioritisation)."""
    roster = Roster()
    live = [a["_id"] for a in col("agents").find({}, {"_id": 1})]
    col("roster").delete_many({"$or": [{"_id": {"$nin": live}}, {"retired_at": {"$ne": None}}]})

    def loop():
        while not agent.stop.is_set():
            try:
                roster.sweep()
            except Exception as e:  # never let the roster thread die
                print(f"[{agent.id}] roster sweep error: {type(e).__name__}: {e}", flush=True)
            agent.stop.wait(SWEEP_S)

    th = threading.Thread(target=loop, name="roster", daemon=True)
    th.start()
    return th
