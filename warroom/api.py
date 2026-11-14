"""War-room HTTP API: the judge's buttons and the read-only inspectors.

Everything here is a thin, legible MongoDB operation:

* approvals  — conditional ``update_one({_id, status:"pending"})``: a decision can land only once.
* scorecard  — two aggregation pipelines (incidents by round, chaos events by run).
* atlas      — collection counts, regular + Atlas Search/Vector indexes, validators, a sample doc.
* doc        — click-to-inspect any document the screen shows.

The chaos package is built in parallel and imported lazily; if it is missing the buttons
answer 503 instead of taking the whole war room down.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from bson import ObjectId
from fastapi import APIRouter, Body, HTTPException
from pymongo import DESCENDING
from pymongo.errors import OperationFailure, PyMongoError

from common import config
from common.db import client, now, swarm, world

from .bson_json import to_json
from .stream import WATCHED

log = logging.getLogger("warroom.api")
router = APIRouter(prefix="/api")

DECISIONS = {"approve": "approved", "approved": "approved", "deny": "denied", "denied": "denied"}
# where to look when a clicked id is not the document _id
ALT_KEYS = {"ledger": ["action_id"], "checkpoints": ["checkpoint_id", "thread_id"],
            "incidents": ["steps.step_id"], "playbooks": ["incident"]}
NEXT_ROUND_CLEAR = ["tasks", "messages"]  # swarm runtime that must not leak into the next night


def _chaos():
    try:
        from chaos import faults, runner  # noqa: PLC0415  (lazy: built in parallel)
    except ImportError as e:
        raise HTTPException(503, f"chaos package not available: {e}") from e
    return runner, faults


_BACKGROUND_SLOTS = threading.BoundedSemaphore(8)  # the API is reachable on the tailnet: cap concurrent controls


def _background(name: str, fn, *args, **kwargs) -> None:
    if not _BACKGROUND_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "too many chaos actions in flight; try again in a few seconds")

    def run():
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("%s failed", name)
        finally:
            _BACKGROUND_SLOTS.release()
    threading.Thread(target=run, name=name, daemon=True).start()


def wait_playbooks_searchable(timeout_s: float = 20.0) -> dict:
    """Atlas Vector Search is eventually consistent: a playbook inserted at the end of night 1 may take
    a moment to appear in the `playbook_vec` index. Before the next night starts, wait (bounded) until
    the newest live playbook is actually returned by $vectorSearch, so round-2 recall is never a coin flip."""
    newest = swarm().playbooks.find_one({"stale": False}, sort=[("created_at", DESCENDING)])
    if not newest or not newest.get("embedding"):
        return {"playbook": None, "searchable": True, "waited_s": 0.0}
    t0 = time.monotonic()
    while True:
        try:
            hits = list(swarm().playbooks.aggregate([
                {"$vectorSearch": {"index": "playbook_vec", "path": "embedding", "queryVector": newest["embedding"],
                                   "numCandidates": 50, "limit": 5,
                                   "filter": {"embedding_model": newest.get("embedding_model"), "stale": False}}},
                {"$project": {"_id": 1}}]))
            if any(h["_id"] == newest["_id"] for h in hits):
                return {"playbook": newest["_id"], "searchable": True, "waited_s": round(time.monotonic() - t0, 2)}
        except PyMongoError as e:
            log.warning("vector readiness probe failed: %s", e)
        if time.monotonic() - t0 >= timeout_s:
            return {"playbook": newest["_id"], "searchable": False, "waited_s": round(time.monotonic() - t0, 2)}
        time.sleep(0.5)


def _db(logical: str):
    names = {"world": config.WORLD_DB, "swarm": config.SWARM_DB}
    if logical in names:
        return client()[names[logical]], logical
    for lg, actual in names.items():
        if actual == logical:
            return client()[actual], lg
    raise HTTPException(404, f"unknown db {logical!r}")


# ---------------------------------------------------------------- judge controls
@router.post("/scenario/{name}")
def start_scenario(name: str, round: int | None = None) -> dict:
    """Start a seeded chaos scenario and mirror the run into swarm.stats{_id:"current"}."""
    runner, _ = _chaos()
    try:
        run_id = runner.run_scenario(name, round=round)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    run = world().runs.find_one({"_id": run_id}) or {}
    current = {"run_id": run_id, "round": run.get("round", round), "scenario": name, "ts": now()}
    swarm().stats.update_one({"_id": "current"}, {"$set": current}, upsert=True)
    return to_json({"ok": True, **current})


@router.post("/chaos/{fault}")
def fire_fault(fault: str, body: dict = Body(default_factory=dict)) -> dict:
    """Manual fault from the launcher. Validated now, executed in the background (the event
    document appears on screen through the change stream the moment it is inserted)."""
    runner, faults = _chaos()
    try:
        f = faults.get(fault)
        target = faults.resolve_target(f, body.get("target"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _background(f"fire-{fault}", runner.fire, fault, target, body.get("params") or {}, manual=True)
    return {"ok": True, "fault": fault, "target": target, "status": "firing"}


@router.post("/kill")
def kill_agent(body: dict = Body(default_factory=dict)) -> dict:
    """SIGKILL a bee. Default selector waits for the worker that just sent a step."""
    runner, faults = _chaos()
    try:
        selector = faults.clean_selector(body.get("selector") or {"role": "worker", "phase": "step_sent"})
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _background("fire-agent_kill", runner.fire, "agent_kill", "swarm", {"selector": selector}, manual=True)
    return {"ok": True, "selector": selector, "status": "hunting"}


@router.post("/approvals/{approval_id}")
def decide(approval_id: str, body: dict = Body(...)) -> dict:
    """The Beekeeper's decision. Conditional on status:"pending" — double clicks and races are no-ops."""
    decision = DECISIONS.get(str(body.get("decision", "")).lower())
    if not decision:
        raise HTTPException(400, "decision must be 'approved' or 'denied'")
    res = swarm().approvals.update_one(
        {"_id": approval_id, "status": "pending"},
        {"$set": {"status": decision, "decided_by": "beekeeper", "decided_at": now()}})
    if res.matched_count == 0:
        doc = swarm().approvals.find_one({"_id": approval_id}, {"status": 1})
        raise HTTPException(404 if doc is None else 409,
                            "no such approval" if doc is None else f"already {doc.get('status')}")
    return {"ok": True, "_id": approval_id, "status": decision}


@router.post("/reset")
def reset(body: dict = Body(default_factory=dict)) -> dict:
    """``full``: wipe all runtime data (playbooks too) — a fresh demo.
    ``next_round``: reset the estate and in-flight work but keep playbooks (the Hive's memory),
    incident history and the chaos/run history the scorecard joins on."""
    mode = body.get("mode", "next_round")
    if mode not in ("full", "next_round"):
        raise HTTPException(400, "mode must be 'full' or 'next_round'")
    out: dict[str, Any] = {"mode": mode}
    try:  # stop pending scenario steps first (reset must still work if chaos isn't importable)
        runner, _ = _chaos()
        out["cancelled_runs"] = runner.cancel()
    except HTTPException:
        out["cancelled_runs"] = []
    if mode == "full":
        # undo the Gardener's deployed upgrade first (systems/mainframe.py back to the legacy code + reload),
        # so the whole demo can be re-run from night 1
        try:
            from gardener import pipeline as gardener_pipeline  # noqa: PLC0415
            out["gardener_revert"] = gardener_pipeline.revert("mainframe")
        except Exception as e:  # never block a reset
            log.exception("gardener revert failed")
            out["gardener_revert"] = f"failed: {e}"
        from scripts.setup_db import reset as wipe  # noqa: PLC0415
        wipe()
        world().counters.delete_many({})  # RUN-/CHX- ids restart at 0001
        # ask the swarm supervisor to restore the original roster (killed workers come back in their role)
        swarm().stats.update_one({"_id": "roster_reset"}, {"$set": {"at": now()}}, upsert=True)
    else:
        w, s = world(), swarm()
        w.op_receipts.delete_many({})
        w.systems.update_many({}, {"$set": {"faults_active": []}})
        t = now()
        out["closed_incidents"] = s.incidents.update_many(
            {"active": True}, {"$set": {"active": False, "status": "failed", "closed_at": t,
                                        "closed_by": "reset"}}).modified_count
        s.approvals.update_many({"status": "pending"},
                                {"$set": {"status": "denied", "decided_by": "reset", "decided_at": t}})
        for c in NEXT_ROUND_CLEAR:
            s[c].delete_many({})
        s.agents.update_many({"status": {"$ne": "dead"}},
                             {"$set": {"target": None, "incident": None, "task": None, "phase": None}})
        w.runs.update_many({"ended_at": None}, {"$set": {"ended_at": t}})
        # a new night starts fully staffed (fair round-2 timing): the supervisor restores the roster
        s.stats.update_one({"_id": "roster_reset"}, {"$set": {"at": t}}, upsert=True)
    if mode == "next_round":
        out["recall_ready"] = wait_playbooks_searchable()
    try:
        r = httpx.post(f"{config.SYSTEMS_URL}/_admin/reset", timeout=30)  # many Atlas round trips under one lock
        out["systems"] = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    except httpx.HTTPError as e:
        out["systems"] = f"unreachable: {e}"
    return to_json({"ok": True, **out})


# ---------------------------------------------------------------- read-only
def scorecard_pipeline() -> list[dict]:
    """Incidents grouped by round — the learning meter (round 2 should be faster: $vectorSearch recall)."""
    return [
        {"$match": {"round": {"$ne": None}}},
        {"$group": {
            "_id": "$round",
            "incidents": {"$sum": 1},
            "resolved": {"$sum": {"$cond": [{"$eq": ["$status", "resolved"]}, 1, 0]}},
            "avg_ttr_s": {"$avg": "$metrics.ttr_s"},
            "avg_diagnosis_s": {"$avg": "$metrics.diagnosis_s"},
            "avg_hops": {"$avg": "$metrics.hops"},
            "avg_attempts": {"$avg": "$metrics.attempts"},
            "avg_steps": {"$avg": "$metrics.steps"},
            "avg_approval_wait_s": {"$avg": "$metrics.approval_wait_s"},
            "avg_takeover_s": {"$avg": "$metrics.takeover_s"},
            "recall_used": {"$sum": {"$cond": [{"$eq": ["$recall.used", True]}, 1, 0]}},
            "best_recall_score": {"$max": {"$max": "$recall.hits.score"}},
            "fault_classes": {"$addToSet": "$fault_class"},
            "runs": {"$addToSet": "$run_id"},
        }},
        {"$sort": {"_id": 1}},
        {"$set": {"round": "$_id"}},
        {"$unset": "_id"},
    ]


def chaos_by_run_pipeline() -> list[dict]:
    return [
        {"$group": {"_id": "$run_id", "round": {"$first": "$round"}, "scenario": {"$first": "$scenario"},
                    "events": {"$sum": 1}, "faults": {"$push": "$fault"},
                    "manual": {"$sum": {"$cond": ["$manual", 1, 0]}}, "first_ts": {"$min": "$ts"}}},
        {"$sort": {"first_ts": 1}},
        {"$set": {"run_id": "$_id"}},
        {"$unset": "_id"},
    ]


@router.get("/scorecard")
def scorecard() -> dict:
    rounds = list(swarm().incidents.aggregate(scorecard_pipeline()))
    runs = list(world().chaos_events.aggregate(chaos_by_run_pipeline()))
    inc_by_run = {r["_id"]: r["n"] for r in swarm().incidents.aggregate(
        [{"$group": {"_id": "$run_id", "n": {"$sum": 1}}}])}
    for r in runs:
        r["incidents"] = inc_by_run.get(r["run_id"], 0)
    for r in rounds:
        for k, v in list(r.items()):
            if isinstance(v, float):
                r[k] = round(v, 2)
    first = next((r for r in rounds if r.get("avg_ttr_s")), None)
    last = next((r for r in reversed(rounds) if r.get("avg_ttr_s")), None)
    speedup = None
    if first and last and first is not last and last["avg_ttr_s"]:
        speedup = round(first["avg_ttr_s"] / last["avg_ttr_s"], 2)
    return to_json({"rounds": rounds, "runs": runs, "speedup": speedup,
                    "pipeline": scorecard_pipeline()})


def _index_info(coll) -> list[dict]:
    out = []
    for ix in coll.list_indexes():
        out.append({"name": ix["name"], "keys": dict(ix["key"]), "unique": ix.get("unique", False),
                    "partial": ix.get("partialFilterExpression"), "ttl_s": ix.get("expireAfterSeconds")})
    return out


def _search_indexes(coll) -> list[dict]:
    try:
        return [{"name": i.get("name"), "type": i.get("type"), "status": i.get("status"),
                 "queryable": i.get("queryable")} for i in coll.list_search_indexes()]
    except (OperationFailure, PyMongoError):
        return []


@router.get("/atlas")
def atlas() -> dict:
    """What lives in MongoDB right now: every collection, its indexes and one real document."""
    out = []
    for logical, colls in WATCHED.items():
        db, _ = _db(logical)
        validators = {c["name"]: bool(c.get("options", {}).get("validator"))
                      for c in db.list_collections(filter={"name": {"$in": colls}})}
        for name in colls:
            coll = db[name]
            sample = coll.find_one(sort=[("_id", DESCENDING)])  # binaries/embeddings are stripped by to_json
            out.append({"db": logical, "db_name": db.name, "coll": name,
                        "count": coll.estimated_document_count(), "validator": validators.get(name, False),
                        "indexes": _index_info(coll) if name in validators else [],
                        "search_indexes": _search_indexes(coll) if name == "playbooks" else [],
                        "sample": sample})
    host = urlsplit(config.MONGODB_URI).hostname or "?"
    port = urlsplit(config.MONGODB_URI).port
    return to_json({"collections": out, "uri_host": f"{host}:{port}" if port else host})


@router.get("/memory")
def memory() -> dict:
    """The Hive's memory, read-only: playbooks (what worked), lessons (what failed verification; FAILURE_MEMORY=1)
    and schema mappings, each with the system version it was learned on, the current version, stale, uses and
    last used. Everything is in MongoDB, so a Nurse that takes over reads exactly this."""
    from swarm import lessons  # noqa: PLC0415
    versions = {d["_id"]: d.get("version") for d in world().systems.find({}, {"version": 1})}
    rows = lessons.items(versions)
    counts: dict[str, dict[str, int]] = {}
    for r in rows:
        c = counts.setdefault(r["kind"], {"total": 0, "stale": 0, "used": 0})
        c["total"] += 1
        c["stale"] += int(bool(r["stale"]))
        c["used"] += int(bool(r.get("uses")))
    return to_json({"failure_memory": lessons.enabled(), "current_versions": versions, "counts": counts,
                    "items": rows})


def _id_candidates(raw: str) -> list[Any]:
    cands: list[Any] = [raw]
    if ObjectId.is_valid(raw):
        cands.append(ObjectId(raw))
    if raw.lstrip("-").isdigit():
        cands.append(int(raw))
    return cands


@router.get("/doc/{db}/{coll}/{doc_id:path}")
def get_doc(db: str, coll: str, doc_id: str) -> dict:
    database, logical = _db(db)
    if coll not in WATCHED.get(logical, []) and coll not in ("counters",):
        raise HTTPException(404, f"{coll!r} is not a war-room collection")
    c = database[coll]
    doc = c.find_one({"_id": {"$in": _id_candidates(doc_id)}})
    matched_on = "_id"
    for key in ALT_KEYS.get(coll, []):
        if doc is not None:
            break
        doc = c.find_one({key: doc_id}, sort=[("_id", DESCENDING)])
        matched_on = key
    if doc is None:
        raise HTTPException(404, f"{logical}.{coll} has no document {doc_id!r}")
    return to_json({"db": logical, "db_name": database.name, "coll": coll, "matched_on": matched_on,
                    "query": {matched_on: doc_id}, "doc": doc})
