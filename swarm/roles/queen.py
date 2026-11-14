"""Queen — owns the goal (all systems green, zero open vulns) and talks to the Beekeeper.

Exactly one queen acts at a time: the queen lease lives in swarm.stats {_id: "queen_lease"} and is taken
with the same atomic claim as any other lease. queen-2 sits on the bench and takes the lease (and the
goal) when queen-1 stops heartbeating.

Active queen, each tick:
  * new open incident -> per-incident graph (thread "<incident>:queen"): triage -> assign an idle scout
  * pending approvals  -> relays them to the Beekeeper (narration + waggle message)
  * status line        -> posts to the Beekeeper when the picture changes
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from common import comms, config
from common.db import col, now
from common.events import say

from .. import leases, policy, scale, settings
from ..harness import Agent, checkpointer, main_loop

LEASE_ID = "queen_lease"


class QueenState(TypedDict, total=False):
    incident: str
    scout: str | None


def build_graph(agent: Agent):
    def triage(state: QueenState, config) -> dict:
        inc = col("incidents").find_one({"_id": state["incident"]})
        affected = ", ".join(sorted(inc.get("affected") or [], key=policy.rank))
        agent.think(f"{inc['_id']} opened ({affected}). Goal stands: every system green, zero open vulns.",
                    incident=inc["_id"], system=inc["system"])
        return {}

    def assign(state: QueenState, config) -> dict:
        cutoff = now() - timedelta(seconds=settings.DEAD_AFTER_S)
        busy = {i["owner"] for i in col("incidents").find({"active": True, "owner": {"$ne": None}}, {"owner": 1})}
        idle = [a["_id"] for a in col("agents").find({"role": "scout", "status": "idle", "last_seen": {"$gte": cutoff},
                                                      "phase": {"$ne": "retiring"}},
                                                     {"_id": 1}).sort("_id", 1) if a["_id"] not in busy]
        if not idle and scale.enabled():  # dynamic scaling: a scout is spawned for this incident
            rid = scale.ensure_scout(agent.id, state["incident"])
            if rid:
                agent.think(f"No free scout for {state['incident']} — spawning one ({rid}).", kind="spawn",
                            incident=state["incident"], pause=False)
                return {"scout": None}
        if not idle:
            agent.think(f"No idle scout for {state['incident']}; the first free scout will self-claim it.",
                        incident=state["incident"], pause=False)
            return {"scout": None}
        r = col("incidents").update_one({"_id": state["incident"], "status": "open", "assigned_to": None},
                                        {"$set": {"assigned_to": idle[0], "assigned_by": agent.id}})
        if r.modified_count:
            agent.think(f"Assigning {state['incident']} to {idle[0]}.", kind="action", incident=state["incident"],
                        pause=False)
            say(agent.id, idle[0], "assign", {"incident": state["incident"]})
        return {"scout": idle[0]}

    g = StateGraph(QueenState)
    g.add_node("triage", triage)
    g.add_node("assign", assign)
    g.add_edge(START, "triage")
    g.add_edge("triage", "assign")
    g.add_edge("assign", END)
    return g.compile(checkpointer=checkpointer())


def run(agent: Agent) -> None:
    graph = build_graph(agent)

    def ensure_lease_doc():
        col("stats").update_one({"_id": LEASE_ID}, {"$setOnInsert": {"owner": None, "lease_until": None,
                                                                     "generation": 0}}, upsert=True)
    started = time.monotonic()
    holding = {"gen": None}
    handled: set[str] = set()
    last_status = {"text": None}

    def hold_crown() -> bool:
        if holding["gen"] is not None:
            try:
                agent.check("stats", LEASE_ID)
                return True
            except leases.LeaseLost:
                holding["gen"] = None
        if agent.standby and time.monotonic() - started < config.LEASE_S:
            return False  # let the primary queen take the crown at boot
        ensure_lease_doc()  # (re)created if a reset wiped swarm.stats
        prev = col("stats").find_one({"_id": LEASE_ID}) or {}
        won = leases.claim(col("stats"), {"_id": LEASE_ID}, agent.id)
        if not won:
            if not holding.get("benched"):
                holding["benched"] = True
                agent.set(status="standby", target="bench")  # another queen holds the crown
            return False
        holding["benched"] = False
        holding["gen"] = won["generation"]
        agent.hold("stats", LEASE_ID, won["generation"])
        agent.set(status="idle", target="hive", phase=None)
        if prev.get("owner") and prev["owner"] != agent.id:
            agent.think(f"{prev['owner']} went silent — {agent.id} takes the queen lease and the goal.",
                        kind="handoff")
            say(agent.id, "beekeeper", "handoff", f"{prev['owner']} down; {agent.id} is now queen.")
        else:
            agent.think("Holding the queen lease. Goal: every system green, zero open vulns.", pause=False)
        return True

    def repair_assignments():
        """Dynamic scaling: an incident handed to a scout that never came up (or retired at that very moment)
        goes back to the queue, so it is re-assigned or a new scout is spawned for it."""
        grace = now() - timedelta(seconds=scale.SPAWN_GRACE_S)
        for inc in col("incidents").find({"active": True, "status": "open", "assigned_to": {"$ne": None}},
                                         {"assigned_to": 1, "assigned_at": 1, "opened_at": 1}):
            if (inc.get("assigned_at") or inc["opened_at"]) > grace:
                continue
            a = col("agents").find_one({"_id": inc["assigned_to"]}, {"status": 1, "phase": 1})
            if a and a.get("status") != "dead" and a.get("phase") != "retiring":
                continue
            r = col("incidents").update_one({"_id": inc["_id"], "status": "open", "assigned_to": inc["assigned_to"]},
                                            {"$set": {"assigned_to": None, "assigned_at": None}})
            if r.modified_count:
                handled.discard(inc["_id"])
                agent.think(f"{inc['assigned_to']} never picked up {inc['_id']}; putting it back in the queue.",
                            incident=inc["_id"], pause=False)

    def tick():
        if not hold_crown():
            return
        if scale.enabled():
            repair_assignments()
        for inc in unassigned_by_priority([i for i in col("incidents").find({"active": True, "status": "open",
                                                                              "assigned_to": None})
                                           if i["_id"] not in handled]):
            handled.add(inc["_id"])
            graph.invoke({"incident": inc["_id"]}, {"configurable": {"thread_id": f"{inc['_id']}:queen"}},
                         durability="exit")
        for apr in col("approvals").find({"status": "pending", "relayed_by": None}):
            r = col("approvals").update_one({"_id": apr["_id"], "relayed_by": None},
                                            {"$set": {"relayed_by": agent.id, "relayed_at": now()}})
            if r.modified_count:
                agent.think(f"Beekeeper: {apr.get('incident') or apr.get('upgrade') or 'a change'} needs your approval — {apr['desc']} ({apr['_id']}).",
                            kind="approval", incident=apr["incident"], pause=False)
                say(agent.id, "beekeeper", "approval", {"approval": apr["_id"], "incident": apr["incident"],
                                                        "desc": apr["desc"], "risk": apr.get("risk")})
        active = list(col("incidents").find({"active": True}, {"status": 1, "owner": 1}).sort("_id", 1))
        text = ("All quiet: every system green." if not active else
                f"{len(active)} active: " + ", ".join(f"{i['_id']} {i['status']} ({i.get('owner') or 'unclaimed'})"
                                                      for i in active))
        if text != last_status["text"]:
            last_status["text"] = text
            say(agent.id, "beekeeper", "status", text)

    def unassigned_by_priority(incs: list[dict]) -> list[dict]:
        """SWARM_COMMS=1 and several waiting: highest priority first (customer-facing, then the prioritised
        feed's top priority for each, then oldest). Otherwise the natural order, unchanged."""
        if not comms.enabled() or len(incs) < 2:
            return incs
        since = min(i.get("opened_at") or now() for i in incs) - timedelta(seconds=60)
        feed = list(col("messages").find({"comms_processed": True, "merged_into": {"$exists": False},
                                          "ts": {"$gte": since}}, {"incident": 1, "system": 1, "priority": 1})
                    .sort("priority", -1).limit(500))
        best = comms.feed_priorities(feed, incs)
        ranked = sorted(incs, key=lambda i: comms.incident_rank(i, best[i["_id"]]))
        if [i["_id"] for i in ranked] != sorted(i["_id"] for i in incs):
            first = ranked[0]
            why = "customer-facing" if comms.customer_facing(first) else f"feed priority {best[first['_id']]}"
            agent.think(f"{len(ranked)} incidents waiting — {first['_id']} first ({why}), then "
                        f"{', '.join(i['_id'] for i in ranked[1:])}.", kind="action", incident=first["_id"],
                        system=first.get("system"), pause=False)
        return ranked

    main_loop(agent, tick, settings.POLL_S)
