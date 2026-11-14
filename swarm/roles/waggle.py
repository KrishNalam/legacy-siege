"""Waggle — the hive's message bus scribe.

One database-level change stream over swarm.{incidents, agents, stats} (heartbeat noise filtered out
server-side in the $match) turns state changes into one-line status and handoff notes in swarm.messages.
The resume token is persisted in swarm.stats {_id: "waggle_resume"}, so a restarted waggle neither
misses nor repeats events.
"""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pymongo.errors import OperationFailure, PyMongoError

from common.db import col, swarm
from common.events import say

from ..harness import Agent, checkpointer, log

PIPELINE = [{"$match": {"$or": [
    {"ns.coll": "incidents"},
    {"ns.coll": "agents", "operationType": "insert"},
    {"ns.coll": "agents", "updateDescription.updatedFields.status": {"$exists": True}},
    {"ns.coll": "stats", "documentKey._id": "queen_lease", "updateDescription.updatedFields.owner": {"$exists": True}},
]}}]


class WaggleState(TypedDict, total=False):
    lines: list[dict]


def interpret(ev: dict) -> list[dict]:
    """Change event -> zero or more bus messages {to, kind, body}."""
    coll, op = ev["ns"]["coll"], ev["operationType"]
    doc = ev.get("fullDocument") or {}
    upd = (ev.get("updateDescription") or {}).get("updatedFields") or {}
    out: list[dict] = []
    if coll == "incidents":
        iid = ev["documentKey"]["_id"]
        if op == "insert":
            out.append({"to": "hive", "kind": "status",
                        "body": f"{iid} opened on {doc.get('system')} (affected: {', '.join(doc.get('affected') or [])})."})
            return out
        if "status" in upd:
            st = upd["status"]
            if st == "resolved":
                m = doc.get("metrics") or {}
                out.append({"to": "beekeeper", "kind": "status",
                            "body": f"{iid} resolved in {m.get('ttr_s', 0):.0f}s — root cause {doc.get('fault_class')} on "
                                    f"{doc.get('root_cause_system')} ({m.get('hops')} hops, {m.get('attempts')} attempts)."})
            elif st == "failed":
                out.append({"to": "beekeeper", "kind": "status", "body": f"{iid} escalated: {doc.get('failure', 'failed')}."})
            else:
                out.append({"to": "hive", "kind": "status", "body": f"{iid} → {st} ({doc.get('owner') or 'unclaimed'})."})
        if "affected" in upd or any(k.startswith("affected.") for k in upd):
            out.append({"to": "hive", "kind": "status",
                        "body": f"{iid} grew: affected now {', '.join(doc.get('affected') or [])}."})
        if any(k == "handoffs" or k.startswith("handoffs.") for k in upd) and doc.get("handoffs"):
            h = doc["handoffs"][-1]
            mid = ""
            for r in h.get("reconciled") or []:
                mid = f" mid-step {r['step_id']}"
            body = (f"{h.get('from')} died{mid} on {h.get('work')}; {h.get('to')} resumed from checkpoint "
                    f"{str(h.get('checkpoint_id') or '')[-8:] or '(start)'}"
                    + (f", skipping verified {', '.join(h['skipped_steps'])}" if h.get("skipped_steps") else "") + ".")
            out.append({"to": "hive", "kind": "handoff", "body": body})
        for k, v in upd.items():
            if k.startswith("steps.") and k.endswith(".status") and v in ("verified", "failed"):
                idx = int(k.split(".")[1])
                st = (doc.get("steps") or [{}] * (idx + 1))[idx]
                word = "verified by the Guard" if v == "verified" else "rejected by the Guard"
                out.append({"to": "hive", "kind": "status",
                            "body": f"{st.get('step_id')} {st.get('op')} {word}: {st.get('evidence') or ''}"[:240]})
        return out
    if coll == "agents":
        aid = ev["documentKey"]["_id"]
        st = upd.get("status") if op == "update" else doc.get("status")
        if st == "dead" and doc.get("phase") != "shutdown":
            out.append({"to": "hive", "kind": "handoff",
                        "body": f"{aid} stopped heartbeating — the Undertaker released its work."})
        elif op == "insert" and doc.get("standby_for"):
            out.append({"to": "hive", "kind": "status", "body": f"{aid} joined the bench (replacing {doc['standby_for']})."})
        return out
    if coll == "stats" and upd.get("owner"):
        out.append({"to": "beekeeper", "kind": "handoff", "body": f"{upd['owner']} now holds the queen lease."})
    return out


def build_graph(agent: Agent):
    def post(state: WaggleState) -> dict:
        for m in state.get("lines") or []:
            say(agent.id, m["to"], m["kind"], m["body"])
        return {}

    g = StateGraph(WaggleState)
    g.add_node("post", post)
    g.add_edge(START, "post")
    g.add_edge("post", END)
    return g.compile(checkpointer=checkpointer())


def run(agent: Agent) -> None:
    graph = build_graph(agent)
    while not agent.stop.is_set():
        token = (col("stats").find_one({"_id": "waggle_resume"}) or {}).get("token")
        try:
            with swarm().watch(PIPELINE, full_document="updateLookup", resume_after=token,
                               max_await_time_ms=500) as stream:
                while not agent.stop.is_set():
                    ev = stream.try_next()
                    if ev is None:
                        continue
                    lines = interpret(ev)
                    if lines:  # only events that say something get a (tiny) graph run
                        graph.invoke({"lines": lines},
                                     {"configurable": {"thread_id": f"waggle:{ev['ns']['coll']}:{ev['documentKey']['_id']}"}},
                                     durability="exit")
                    col("stats").update_one({"_id": "waggle_resume"}, {"$set": {"token": stream.resume_token}},
                                            upsert=True)
        except OperationFailure as e:
            log(agent.id, f"resume failed ({e.code}); starting a fresh stream")
            col("stats").delete_one({"_id": "waggle_resume"})
        except PyMongoError as e:
            log(agent.id, f"stream error: {e}")
            agent.nap(1.0)
