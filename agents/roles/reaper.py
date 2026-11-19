"""Reaper: marks agents whose heartbeat went stale as dead and releases their work.

Agents whose last_seen is older than DEAD_AFTER_S -> status dead; their task and incident leases are released
(lease_until -> past, owner kept so recovery agents know whose work it was) and the coordinator lease is freed if
they held it; a reporter note goes to the recovery agents. Runs inside the monitor's loop (same process), as a
small LangGraph graph on an event-scoped thread per dead agent and generation.
"""
from __future__ import annotations

from datetime import timedelta
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pymongo import ReturnDocument

from common.db import col, now
from common.events import say

from .. import leases, settings
from ..harness import Agent


class ReapState(TypedDict, total=False):
    agent: str
    last_seen: str
    released: dict
    confirmed: bool


def build(agent: Agent, cp):
    def confirm_dead(state: ReapState, config) -> dict:
        cutoff = now() - timedelta(seconds=settings.DEAD_AFTER_S)
        doc = col("agents").find_one_and_update(
            {"_id": state["agent"], "status": {"$ne": "dead"}, "last_seen": {"$lt": cutoff}},
            {"$set": {"status": "dead", "died_at": now(), "phase": None}}, return_document=ReturnDocument.AFTER)
        return {"confirmed": doc is not None}

    def release_leases(state: ReapState, config) -> dict:
        if not state.get("confirmed"):
            return {}
        a = state["agent"]
        tasks = [t["_id"] for t in col("tasks").find({"owner": a, "status": "claimed"}, {"_id": 1})]
        incs = [i["_id"] for i in col("incidents").find({"owner": a, "active": True}, {"_id": 1})]
        col("tasks").update_many({"owner": a, "status": "claimed"}, {"$set": {"lease_until": leases.EPOCH}})
        col("incidents").update_many({"owner": a, "active": True}, {"$set": {"lease_until": leases.EPOCH}})
        q = col("stats").update_one({"_id": "coordinator_lease", "owner": a}, {"$set": {"lease_until": leases.EPOCH}})
        return {"released": {"tasks": tasks, "incidents": incs, "coordinator_lease": bool(q.modified_count)}}

    def notify(state: ReapState, config) -> dict:
        if not state.get("confirmed"):
            return {}
        rel = state.get("released") or {}
        work = rel.get("tasks", []) + rel.get("incidents", []) + (["the coordinator lease"] if rel.get("coordinator_lease") else [])
        text = (f"{state['agent']} stopped heartbeating — marked dead; released {', '.join(work)}."
                if work else f"{state['agent']} stopped heartbeating — marked dead (held no work).")
        agent.think(text, kind="handoff", incident=(rel.get("incidents") or [None])[0], pause=False)
        say(agent.id, "recovery", "handoff", {"dead": state["agent"], "released": rel, "text": text})
        return {}

    g = StateGraph(ReapState)
    g.add_node("confirm_dead", confirm_dead)
    g.add_node("release_leases", release_leases)
    g.add_node("notify", notify)
    g.add_edge(START, "confirm_dead")
    g.add_edge("confirm_dead", "release_leases")
    g.add_edge("release_leases", "notify")
    g.add_edge("notify", END)
    return g.compile(checkpointer=cp)


def sweep(agent: Agent, reap) -> None:
    """One pass: reap every other agent whose heartbeat is older than DEAD_AFTER_S."""
    cutoff = now() - timedelta(seconds=settings.DEAD_AFTER_S)
    for a in col("agents").find({"status": {"$ne": "dead"}, "last_seen": {"$lt": cutoff},
                                 "_id": {"$ne": agent.id}}, {"_id": 1, "generation": 1}):
        reap.invoke({"agent": a["_id"]},
                    {"configurable": {"thread_id": f"reaper:{a['_id']}:{a.get('generation', 0)}"}},
                    durability="exit")
