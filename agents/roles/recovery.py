"""Recovery bee — standby on the bench; takes over work whose owner died.

Takeover of a TASK (operator died mid-fix), as a LangGraph graph on thread "recovery:<task>:<generation>":
    load_checkpoint  -> read the dead operator's LangGraph checkpoint (thread = task id) from MongoDB
    reconcile        -> for every step stuck in 'sent': READ BACK /state first.
                        already applied -> leave it for the Verifier (no resend)
                        not applied     -> resend with the SAME action_id (the system dedupes receipts)
    record_handoff   -> incident.handoffs[] += {from, to, ts, reason, checkpoint_id, skipped_steps, takeover_s}
then the recovery resumes the operator graph from that checkpoint, as the new lease holder (generation+1).

Takeover of an INCIDENT (investigator died): same idea with the investigator graph (thread "<incident>:investigator").
Recovery agents also pick up open work no live specialist/investigator can take.
"""
from __future__ import annotations

from datetime import timedelta
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from common import config as shared
from common.db import col, now, txn

from .. import leases, scale, settings
from ..harness import Agent, checkpointer, main_loop
from . import investigator as investigator_role
from . import operator as operator_role


def _orphans(coll: str, match: dict) -> list[dict]:
    """Leased work whose lease expired AND whose holder is dead (or gone) — a $lookup into agents."""
    return list(col(coll).aggregate([
        {"$match": {**match, "lease_until": {"$lt": now()}}},
        {"$lookup": {"from": "agents", "localField": "owner", "foreignField": "_id", "as": "holder"}},
        # holder dead/gone — or alive but it abandoned the work (stopped renewing a full LEASE_S ago,
        # e.g. it gave up after repeated errors); either way the work must not stay stranded
        {"$match": {"$or": [{"holder.status": "dead"}, {"holder": {"$size": 0}},
                            {"lease_until": {"$lt": now() - timedelta(seconds=shared.LEASE_S)}}]}},
        {"$sort": {"ts": 1}},
        {"$limit": 5},
    ]))


class NurseState(TypedDict, total=False):
    kind: str               # "task" | "incident"
    id: str
    incident: str
    prev_owner: str | None
    generation: int
    cp_id: str | None
    resume_at: list[str]
    skipped: list[str]
    reconciled: list[dict]
    takeover_s: float


def build_graph(agent: Agent, operator_graph, investigator_graph):

    def load_checkpoint(state: NurseState, config) -> dict:
        if state["kind"] == "task":
            g, thread = operator_graph, state["id"]
            task = col("tasks").find_one({"_id": state["id"]})
            inc = col("incidents").find_one({"_id": task["incident"]}, {"steps": 1})
            skipped = [s["step_id"] for s in inc.get("steps", [])
                       if s["step_id"] in task.get("step_ids", []) and s["status"] == "verified"]
        else:
            g, thread = investigator_graph, f"{state['id']}:investigator"
            inc = col("incidents").find_one({"_id": state["id"]}, {"steps": 1})
            skipped = [s["step_id"] for s in inc.get("steps", []) if s["status"] == "verified"]
        snap = g.get_state({"configurable": {"thread_id": thread}})
        cp_id = snap.config["configurable"].get("checkpoint_id") if snap.values else None
        resume_at = list(snap.next)
        prev = col("agents").find_one({"_id": state.get("prev_owner")}) or {}
        takeover_s = round((now() - prev["last_seen"]).total_seconds(), 2) if prev.get("last_seen") else 0.0
        where = f"'{resume_at[0]}'" if resume_at else "the start"
        agent.think(f"Loaded {state.get('prev_owner')}'s checkpoint {str(cp_id)[-8:] if cp_id else '(none)'} for "
                    f"{state['id']} from MongoDB: resuming at {where}"
                    + (f"; {', '.join(skipped)} already verified." if skipped else "."),
                    kind="handoff", incident=state["incident"])
        return {"cp_id": cp_id, "resume_at": resume_at, "skipped": skipped, "takeover_s": takeover_s}

    def reconcile(state: NurseState, config) -> dict:
        if state["kind"] != "task":
            return {"reconciled": []}
        task = col("tasks").find_one({"_id": state["id"]})
        inc = col("incidents").find_one({"_id": task["incident"]}, {"steps": 1})
        out = []
        for st in inc.get("steps", []):
            if st["step_id"] not in task["step_ids"] or st["status"] != "sent":
                continue
            out.append(operator_role.reconcile_sent(agent, task, state["generation"], st, adopt=True))
        return {"reconciled": out}

    def record_handoff(state: NurseState, config) -> dict:
        h = {"from": state.get("prev_owner"), "to": agent.id, "ts": now(), "reason": "owner dead (lease expired)",
             "kind": state["kind"], "work": state["id"], "checkpoint_id": state.get("cp_id"),
             "resume_at": state.get("resume_at"), "skipped_steps": state.get("skipped") or [],
             "reconciled": state.get("reconciled") or [], "takeover_s": state.get("takeover_s")}
        lease_coll = "tasks" if state["kind"] == "task" else "incidents"

        def fn(s):
            leases.fence(col(lease_coll), state["id"], agent.id, state["generation"], s)
            col("incidents").update_one({"_id": state["incident"]}, {"$push": {"handoffs": h}}, session=s)
        txn(fn)
        agent.think(f"Took over {state['id']} from {state.get('prev_owner')} after {state.get('takeover_s', 0):.0f}s "
                    f"(generation {state['generation']}).", kind="handoff", incident=state["incident"])
        return {}

    g = StateGraph(NurseState)
    g.add_node("load_checkpoint", load_checkpoint)
    g.add_node("reconcile", reconcile)
    g.add_node("record_handoff", record_handoff)
    g.add_edge(START, "load_checkpoint")
    g.add_edge("load_checkpoint", "reconcile")
    g.add_edge("reconcile", "record_handoff")
    g.add_edge("record_handoff", END)
    return g.compile(checkpointer=checkpointer())


def _live(role: str, **extra) -> bool:
    cutoff = now() - timedelta(seconds=settings.DEAD_AFTER_S)
    return col("agents").count_documents({"role": role, "status": {"$ne": "dead"}, "last_seen": {"$gte": cutoff},
                                          **extra}, limit=1) > 0


def run(agent: Agent) -> None:
    operator_graph = operator_role.build_graph(agent)
    investigator_graph = investigator_role.build_graph(agent)
    graph = build_graph(agent, operator_graph, investigator_graph)

    def take_task(t: dict) -> bool:
        won = leases.takeover(col("tasks"), t["_id"], t.get("owner"), t["generation"], agent.id)
        if not won:
            return False  # another recovery got there first
        agent.hold("tasks", won["_id"], won["generation"])
        try:
            agent.set(status="working", target=won.get("system") or "hive", incident=won["incident"],
                      task=won["_id"], phase="takeover")
            graph.invoke({"kind": "task", "id": won["_id"], "incident": won["incident"], "prev_owner": t.get("owner"),
                          "generation": won["generation"]},
                         {"configurable": {"thread_id": f"recovery:{won['_id']}:{won['generation']}"}}, durability="sync")
            operator_role.execute(agent, operator_graph, won, resume=True)
        finally:
            agent.drop("tasks", won["_id"])
            agent.rest()
        return True

    def take_incident(i: dict) -> bool:
        won = leases.takeover(col("incidents"), i["_id"], i.get("owner"), i["generation"], agent.id)
        if not won:
            return False
        agent.hold("incidents", won["_id"], won["generation"])
        try:
            agent.set(status="working", target=won["system"], incident=won["_id"], phase="takeover")
            graph.invoke({"kind": "incident", "id": won["_id"], "incident": won["_id"], "prev_owner": i.get("owner"),
                          "generation": won["generation"]},
                         {"configurable": {"thread_id": f"recovery:{won['_id']}:{won['generation']}"}}, durability="sync")
            investigator_role.drive(agent, investigator_graph, won["_id"], won["generation"])
        finally:
            agent.drop("incidents", won["_id"])
            agent.rest()
        return True

    def tick():
        # 1. orphaned tasks (operator died mid-task)
        for t in _orphans("tasks", {"status": "claimed"}):
            if take_task(t):
                return
        # 2. orphaned incidents (investigator died mid-incident)
        for i in _orphans("incidents", {"active": True, "status": {"$nin": ["open", "resolved", "failed"]}}):
            if take_incident(i):
                return
        # 3. open work nobody alive can take (e.g. the only mainframe operator is dead)
        stale = now() - timedelta(seconds=settings.RECOVERY_PICKUP_S)
        # a task whose specialty has no live operator at all needs no grace period beyond a moment
        orphan_after = now() - timedelta(seconds=min(settings.RECOVERY_PICKUP_S, 0.5))
        for t in col("tasks").find({"status": "open", "ts": {"$lt": orphan_after}}).sort("ts", 1).limit(5):
            if _live("operator", specialty=t["specialty"]) or scale.spawn_pending(task_id=t["_id"]):
                continue
            won = leases.claim(col("tasks"), {"_id": t["_id"], "status": "open"}, agent.id,
                               extra_set={"status": "claimed", "claimed_at": now()})
            if won:
                agent.think(f"No live {t['specialty']} operator — {agent.id} takes {t['_id']}.", kind="handoff",
                            incident=t["incident"])
                operator_role.execute(agent, operator_graph, won)
                return
        if not _live("investigator"):
            q = {"active": True, "status": "open", "opened_at": {"$lt": stale}}
            if scale.enabled():  # leave an incident alone while the investigator spawned for it is on its way
                busy = [r["incident"] for r in col("spawn_requests").find(
                    {"role": "investigator", "status": {"$in": ["pending", "spawned"]},
                     "ts": {"$gte": now() - timedelta(seconds=scale.SPAWN_GRACE_S)}}, {"incident": 1})]
                q["_id"] = {"$nin": busy}
            inc = leases.claim(col("incidents"), q,
                               agent.id, extra_set={"status": "claimed", "claimed_at": now()})
            if inc:
                agent.hold("incidents", inc["_id"], inc["generation"])
                try:
                    agent.think(f"No live investigator — {agent.id} takes {inc['_id']}.", kind="handoff", incident=inc["_id"])
                    investigator_role.drive(agent, investigator_graph, inc["_id"], inc["generation"])
                finally:
                    agent.drop("incidents", inc["_id"])
                    agent.rest()

    main_loop(agent, tick, settings.RECOVERY_POLL_S)
