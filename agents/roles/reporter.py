"""Reporter — the hive's message bus scribe.

One database-level change stream over swarm.{incidents, agents, stats} (heartbeat noise filtered out
server-side in the $match) turns state changes into one-line status and handoff notes in swarm.messages.
The resume token is persisted in swarm.stats {_id: "reporter_resume"}, so a restarted reporter neither
misses nor repeats events.
"""
from __future__ import annotations

import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pymongo.errors import OperationFailure, PyMongoError

from common import config
from common.db import col, swarm
from common.events import say, think
from common import comms

from .. import roster

from ..harness import Agent, checkpointer, log

PIPELINE = [{"$match": {"$or": [
    {"ns.coll": "incidents"},
    {"ns.coll": "agents", "operationType": "insert"},
    {"ns.coll": "agents", "updateDescription.updatedFields.status": {"$exists": True}},
    {"ns.coll": "stats", "documentKey._id": "coordinator_lease", "updateDescription.updatedFields.owner": {"$exists": True}},
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
                out.append({"to": "approver", "kind": "status",
                            "body": f"{iid} resolved in {m.get('ttr_s', 0):.0f}s — root cause {doc.get('fault_class')} on "
                                    f"{doc.get('root_cause_system')} ({m.get('hops')} hops, {m.get('attempts')} attempts)."})
            elif st == "failed":
                out.append({"to": "approver", "kind": "status", "body": f"{iid} escalated: {doc.get('failure', 'failed')}."})
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
                word = "verified by the Verifier" if v == "verified" else "rejected by the Verifier"
                out.append({"to": "hive", "kind": "status",
                            "body": f"{st.get('step_id')} {st.get('op')} {word}: {st.get('evidence') or ''}"[:240]})
        return out
    if coll == "agents":
        aid = ev["documentKey"]["_id"]
        st = upd.get("status") if op == "update" else doc.get("status")
        if st == "dead" and doc.get("phase") != "shutdown":
            out.append({"to": "hive", "kind": "handoff",
                        "body": f"{aid} stopped heartbeating — the reaper released its work."})
        elif op == "insert" and doc.get("standby_for"):
            out.append({"to": "hive", "kind": "status", "body": f"{aid} joined the bench (replacing {doc['standby_for']})."})
        return out
    if coll == "stats" and upd.get("owner"):
        out.append({"to": "approver", "kind": "handoff", "body": f"{upd['owner']} now holds the coordinator lease."})
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
    if config.MONGO_SPINE:  # chain of custody from change-stream pre-images (agents/custody.py), same process
        from .. import custody  # noqa: PLC0415
        custody.start(agent.stop, lambda text, row: think(agent.id, "reporter", text, incident=row.get("incident"),
                                                           kind="handoff"))
    if comms.enabled():
        roster.start(agent)  # live roster of every agent beside the prioritised bus (agents/roster.py)
        return run_prioritiser(agent)
    graph = build_graph(agent)
    while not agent.stop.is_set():
        token = (col("stats").find_one({"_id": "reporter_resume"}) or {}).get("token")
        try:
            with swarm().watch(PIPELINE, full_document="updateLookup", resume_after=token,
                               max_await_time_ms=500) as stream:
                while not agent.stop.is_set():
                    agent.pause_point()
                    ev = stream.try_next()
                    if ev is None:
                        continue
                    lines = interpret(ev)
                    if lines:  # only events that say something get a (tiny) graph run
                        graph.invoke({"lines": lines},
                                     {"configurable": {"thread_id": f"reporter:{ev['ns']['coll']}:{ev['documentKey']['_id']}"}},
                                     durability="exit")
                    col("stats").update_one({"_id": "reporter_resume"}, {"$set": {"token": stream.resume_token}},
                                            upsert=True)
        except OperationFailure as e:
            log(agent.id, f"resume failed ({e.code}); starting a fresh stream")
            col("stats").delete_one({"_id": "reporter_resume"})
        except PyMongoError as e:
            log(agent.id, f"stream error: {e}")
            agent.nap(1.0)


BATCH_MAX, BATCH_S = 256, 0.5  # one prioritiser batch: at most 256 changes or 0.5 s
LOG_THOUGHT_S = 5.0  # a batch of estate log lines only narrates "Comms: ..." at most this often
LOGS_RESUME = 'reporter_logs_resume'


def drain(streams, limit=BATCH_MAX, budget_s=BATCH_S):
    """One bounded batch from several change streams, round-robin so an estate log flood never starves the
    hive's own events: [(name, event)], at most `limit` long. A stream that has nothing left drops out."""
    live, out, start = list(streams), [], time.monotonic()
    while live and len(out) < limit:
        for name, stream in list(live):
            ev = stream.try_next()
            if ev is None:
                live.remove((name, stream))
                continue
            out.append((name, ev))
            if len(out) >= limit or time.monotonic() - start >= budget_s:
                return out
    return out


def log_lines(docs):
    """world.system_logs inserts -> comms inputs (source=<system>), tagged with the active incident whose
    blast radius holds that system (when exactly one does)."""
    owner, seen = {}, set()
    for inc in col('incidents').find({'active': True}, {'affected': 1, 'system': 1}):
        for sysid in set(inc.get('affected') or []) | {inc.get('system')}:
            owner[sysid] = None if sysid in seen else inc['_id']
            seen.add(sysid)
    return [m for m in (comms.from_log(d, owner.get(d.get('system'))) for d in docs) if m]


def post_batch(agent, lines, *, narrate=True):
    waiting = {a.get('incident') for a in col('approvals').find({'status': 'pending'}, {'incident': 1})}
    groups = comms.prioritise(lines, waiting - {None})
    for m in groups:
        stored = [x['_id'] for x in m['members'] if '_id' in x]
        fields = {k: m[k] for k in ('priority', 'source', 'incident', 'system', 'severity', 'count')}
        if stored:
            col('messages').update_one({'_id': stored[0]}, {'$set': {**fields, 'comms_processed': True}})
            if len(stored) > 1:
                col('messages').update_many({'_id': {'$in': stored[1:]}},
                    {'$set': {'comms_processed': True, 'merged_into': stored[0]}})
        else:
            say(agent.id, m['to'], m['kind'], m['body'], incident=m['incident'], source=m['source'],
                system=m['system'], severity=m['severity'], count=m['count'], processed=True, priority=m['priority'])
    if groups:
        n = sum(m['count'] for m in groups)
        n_logs = sum(m['count'] for m in groups if m['kind'] == 'log')
        if narrate:
            text = f"Comms: {n} messages, {n - len(groups)} merged, top: {str(groups[0]['body'])[:140]}"
            think(agent.id, 'reporter', text, kind='comms')
        col('stats').update_one({'_id': 'comms'}, {'$inc': {'messages': n, 'merged': n - len(groups), 'logs': n_logs},
            '$set': {'top': str(groups[0]['body'])[:140]}}, upsert=True)
    return groups


def run_prioritiser(agent):
    """Bounded batches on the existing agent over two streams: the swarm DB (hive events + messages) and
    world.system_logs (WARN/ERROR/ABEND estate lines). Each stream's token is saved after its batch is posted."""
    pipeline = [{'$match': {'$or': PIPELINE[0]['$match']['$or'] + [
        {'ns.coll': 'messages', 'operationType': 'insert', 'fullDocument.comms_processed': {'$ne': True}},
        {'ns.coll': 'approvals', 'operationType': 'insert'},
    ]}}]
    last_log_thought = 0.0
    while not agent.stop.is_set():
        token = (col('stats').find_one({'_id': 'reporter_resume'}) or {}).get('token')
        log_token = (col('stats').find_one({'_id': LOGS_RESUME}) or {}).get('token')
        try:
            with swarm().watch(pipeline, full_document='updateLookup', resume_after=token,
                               max_await_time_ms=400) as stream, \
                    col('system_logs').watch(comms.LOG_PIPELINE, resume_after=log_token,
                                             max_await_time_ms=100) as logs:
                while not agent.stop.is_set():
                    agent.pause_point()
                    lines, log_docs = [], []
                    for name, ev in drain([('swarm', stream), ('logs', logs)]):
                        doc = ev.get('fullDocument') or {}
                        if name == 'logs':
                            log_docs.append(doc)
                            continue
                        coll = ev['ns']['coll']
                        if coll == 'messages':
                            lines.append(doc)
                        elif coll == 'approvals':
                            if doc.get('status') == 'pending':
                                lines.append(dict(to='approver', kind='approval', body='Human approval waiting',
                                                  incident=doc.get('incident'), severity='warning', source='approvals'))
                        else:
                            for m in interpret(ev):
                                m.update(source=coll, incident=ev['documentKey']['_id'] if coll == 'incidents'
                                         else doc.get('incident'), system=doc.get('system'))
                                lines.append(m)
                    hive_lines = bool(lines)
                    lines += log_lines(log_docs) if log_docs else []
                    if lines:
                        narrate = hive_lines or time.monotonic() - last_log_thought >= LOG_THOUGHT_S
                        post_batch(agent, lines, narrate=narrate)
                        if narrate and not hive_lines:
                            last_log_thought = time.monotonic()
                    for doc_id, s in (('reporter_resume', stream), (LOGS_RESUME, logs)):
                        if s.resume_token:
                            col('stats').update_one({'_id': doc_id}, {'$set': {'token': s.resume_token}}, upsert=True)
        except OperationFailure as e:
            log(agent.id, f'resume failed ({e.code}); starting fresh streams')
            col('stats').delete_many({'_id': {'$in': ['reporter_resume', LOGS_RESUME]}})
        except PyMongoError as e:
            log(agent.id, f'stream error: {e}')
            agent.nap(1.0)
