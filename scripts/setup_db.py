"""Create collections, validators and indexes. Idempotent.

    .venv/bin/python -m scripts.setup_db            # ensure everything exists
    .venv/bin/python -m scripts.setup_db --reset    # wipe runtime data (keeps chaos_scenarios)
"""
from __future__ import annotations

import sys
import time

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import CollectionInvalid, OperationFailure
from pymongo.operations import SearchIndexModel

from common import config
from common.db import swarm, world

STEP_STATUSES = ["planned", "sent", "verified", "failed", "unknown"]

INCIDENT_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "status", "system", "opened_at"],
        "properties": {
            "status": {"enum": ["open", "claimed", "diagnosing", "fixing", "verifying",
                                "awaiting_approval", "resolved", "failed"]},
            "steps": {
                "bsonType": "array",
                "items": {
                    "bsonType": "object",
                    "required": ["step_id", "status", "op", "system"],
                    "properties": {"status": {"enum": STEP_STATUSES}},
                    # a verified step MUST carry evidence (the Guard bee's read-back)
                    "anyOf": [
                        {"properties": {"status": {"enum": [s for s in STEP_STATUSES if s != "verified"]}}},
                        {"required": ["evidence"], "properties": {"evidence": {"bsonType": "string", "minLength": 1}}},
                    ],
                },
            },
        },
    }
}

LEDGER_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["action_id", "incident", "agent", "op", "status", "ts"],
        "properties": {"status": {"enum": STEP_STATUSES}},
    }
}

AGENT_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "role", "status"],
        "properties": {
            "role": {"enum": ["queen", "scout", "worker", "guard", "waggle", "sentinel", "nurse", "gardener"]},
            "status": {"enum": ["idle", "working", "standby", "dead"]},
        },
    }
}


# ---- MONGO_SPINE=1 (W25): validators on the lease/approval/upgrade/memory collections. validationLevel
# "moderate": inserts and updates of valid docs are checked; pre-existing docs are left alone. Enums come from
# a grep of every writer (swarm/, gardener/, warroom/, chaos/); upgrade_requests statuses stay open (W19/W23).
TASK_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "incident", "kind", "specialty", "status", "generation"],
        "properties": {
            "status": {"enum": ["open", "claimed", "done", "failed"]},
            "generation": {"bsonType": ["int", "long"], "minimum": 0},  # the fencing token
            "owner": {"bsonType": ["string", "null"]},
            "lease_until": {"bsonType": ["date", "null"]},
            "step_ids": {"bsonType": "array", "items": {"bsonType": "string"}},
        },
    }
}
APPROVAL_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "kind", "status", "ts"],
        "properties": {
            "kind": {"enum": ["step", "upgrade"]},
            "status": {"enum": ["pending", "approved", "denied"]},
            # a decision always names who decided (beekeeper / autoplay / reset)
        },
        "anyOf": [{"properties": {"status": {"enum": ["pending"]}}},
                  {"required": ["decided_by", "decided_at"],
                   "properties": {"decided_by": {"bsonType": "string"}, "decided_at": {"bsonType": "date"}}}],
    }
}
UPGRADE_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "system", "status"],
        "properties": {"system": {"bsonType": "string"}, "status": {"bsonType": "string"},
                       "evidence": {"bsonType": ["array", "null"]}},
    }
}
PLAYBOOK_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "system", "fault_class", "embedding", "embedding_model", "stale", "system_version"],
        "properties": {
            # a vector of the wrong size is silently skipped by the vector index; reject it at the door instead
            "embedding": {"bsonType": "array", "minItems": config.EMBED_DIM, "maxItems": config.EMBED_DIM},
            "embedding_model": {"bsonType": "string"},
            "stale": {"bsonType": "bool"},
            "system_version": {"bsonType": ["int", "long"]},
        },
    }
}
SPINE_VALIDATORS = {"tasks": TASK_SCHEMA, "approvals": APPROVAL_SCHEMA, "upgrade_requests": UPGRADE_SCHEMA,
                    "playbooks": PLAYBOOK_SCHEMA}
# change-stream pre-images: every update keeps the document as it was, so the custody watcher (swarm/custody.py)
# can show "before -> after" for each lease change. Not on agents (heartbeats every few seconds = churn).
PRE_IMAGE_COLLECTIONS = ["tasks", "incidents", "approvals"]


def ensure_spine(s) -> None:
    """MONGO_SPINE=1 extras on the swarm DB. Idempotent; MongoDB 6.0+ (local atlas-local 8.0 and Atlas)."""
    for name, validator in SPINE_VALIDATORS.items():
        _ensure(s, name)
        s.command("collMod", name, validator=validator, validationLevel="moderate")
    for name in PRE_IMAGE_COLLECTIONS:
        s.command("collMod", name, changeStreamPreAndPostImages={"enabled": True})
    _ensure(s, "custody")
    s.custody.create_indexes([
        IndexModel([("doc_id", ASCENDING), ("seq", DESCENDING)], name="chain_per_doc", unique=True),
        IndexModel([("incident", ASCENDING), ("ts", ASCENDING)]),
        IndexModel([("ts", ASCENDING)], expireAfterSeconds=86400, name="ttl_24h"),
    ])


def _ensure(db, name, validator=None):
    try:
        if validator:
            db.create_collection(name, validator=validator, validationLevel="strict")
        else:
            db.create_collection(name)
    except CollectionInvalid:
        if validator:
            db.command("collMod", name, validator=validator, validationLevel="strict")


def ensure() -> None:
    w, s = world(), swarm()
    for n in ["systems", "system_state", "system_logs", "op_receipts", "chaos_scenarios", "chaos_events", "runs"]:
        _ensure(w, n)
    _ensure(s, "incidents", INCIDENT_SCHEMA)
    _ensure(s, "ledger", LEDGER_SCHEMA)
    _ensure(s, "agents", AGENT_SCHEMA)
    for n in ["tasks", "thoughts", "messages", "playbooks", "approvals", "upgrade_requests", "stats", "counters",
              "schema_samples", "schema_mappings", "spawn_requests"]:
        _ensure(s, n)
    # dynamic scaling (SWARM_DYNAMIC): the supervisor polls pending requests; history expires after a day
    s.spawn_requests.create_index([("status", ASCENDING), ("ts", ASCENDING)])
    s.spawn_requests.create_index([("ts", ASCENDING)], expireAfterSeconds=86400, name="ttl_24h")

    w.system_logs.create_indexes([
        IndexModel([("system", ASCENDING), ("ts", ASCENDING)]),
        IndexModel([("ts", ASCENDING)], expireAfterSeconds=86400, name="ttl_24h"),
    ])
    w.system_state.create_index([("system", ASCENDING), ("kind", ASCENDING)])
    w.chaos_events.create_index([("ts", DESCENDING)])
    w.chaos_events.create_index([("run_id", ASCENDING), ("ts", ASCENDING)])
    w.runs.create_index([("ended_at", ASCENDING), ("started_at", DESCENDING)])
    # slow_apply: the tick loop claims due delayed ops; partial index = only the not-yet-applied ones
    w.op_receipts.create_index([("apply_at", ASCENDING)], name="pending_delayed_ops",
                               partialFilterExpression={"applied_at": None, "apply_at": {"$type": "date"}})

    # one active incident per correlation key (cascade coalescing); partial index = only active docs
    s.incidents.create_index([("correlation_key", ASCENDING)], name="one_active_per_key", unique=True,
                             partialFilterExpression={"active": True})
    s.incidents.create_index([("status", ASCENDING)], name="active_incidents",
                             partialFilterExpression={"active": True})
    s.incidents.create_index([("opened_at", DESCENDING)])
    s.ledger.create_index([("action_id", ASCENDING)], unique=True, name="exactly_once")
    s.ledger.create_index([("incident", ASCENDING), ("ts", ASCENDING)])
    s.agents.create_index([("role", ASCENDING), ("status", ASCENDING), ("last_seen", ASCENDING)])
    s.thoughts.create_indexes([
        IndexModel([("ts", ASCENDING)], expireAfterSeconds=86400, name="ttl_24h"),
        IndexModel([("incident", ASCENDING), ("ts", ASCENDING)]),
    ])
    s.messages.create_index([("ts", ASCENDING)], expireAfterSeconds=86400, name="ttl_24h")
    from common import comms
    if comms.enabled():
        _ensure(s, "gardener_notes")
        s.messages.create_index([("priority", DESCENDING), ("incident", ASCENDING), ("ts", ASCENDING)],
                                name="comms_priority_incident")
    from gardener import help as gardener_help
    if gardener_help.enabled():  # GARDENER_HELP=1: Sentinel #2 / Worker requests answered by the Gardener
        _ensure(s, "gardener_notes")
        _ensure(s, "gardener_requests")
        s.gardener_requests.create_index([("status", ASCENDING), ("ts", ASCENDING)], name="open_requests")
    # schema drift: observed feed samples expire; the known-good sample is re-written every few seconds
    s.schema_samples.create_index([("ts", ASCENDING)], expireAfterSeconds=86400, name="ttl_24h")
    s.tasks.create_index([("incident", ASCENDING)])
    s.tasks.create_index([("status", ASCENDING), ("lease_until", ASCENDING)])
    s.tasks.create_index([("status", ASCENDING), ("specialty", ASCENDING), ("ts", ASCENDING)])
    s.incidents.create_index([("steps.status", ASCENDING)], name="active_step_status",
                             partialFilterExpression={"active": True})
    s.incidents.create_index([("run_id", ASCENDING)])
    s.approvals.create_index([("status", ASCENDING), ("ts", ASCENDING)])
    s.approvals.create_index([("incident", ASCENDING), ("step_id", ASCENDING)])
    s.upgrade_requests.create_index([("status", ASCENDING)])
    s.upgrade_requests.create_index([("system", ASCENDING), ("fault_class", ASCENDING)], unique=True,
                                    name="one_upgrade_per_weakness")
    s.playbooks.create_index([("system", ASCENDING), ("stale", ASCENDING), ("system_version", ASCENDING)])
    # failure memory (swarm/lessons.py, FAILURE_MEMORY=1): attempts that failed verification, version-tagged
    from swarm.lessons import INDEXES as LESSON_INDEXES, LESSON_SCHEMA  # noqa: PLC0415
    _ensure(s, "lessons", LESSON_SCHEMA)
    s.lessons.create_indexes([IndexModel(keys, **opts) for keys, opts in LESSON_INDEXES])
    if config.MONGO_SPINE:
        ensure_spine(s)

    # Atlas Vector Search over playbooks ("honey"), pre-filterable by system/class/staleness
    existing = {i["name"] for i in s.playbooks.list_search_indexes()}
    if "playbook_vec" not in existing:
        s.playbooks.create_search_index(SearchIndexModel(name="playbook_vec", type="vectorSearch", definition={
            "fields": [
                {"type": "vector", "path": "embedding", "numDimensions": config.EMBED_DIM, "similarity": "cosine"},
                {"type": "filter", "path": "system"},
                {"type": "filter", "path": "fault_class"},
                {"type": "filter", "path": "stale"},
                {"type": "filter", "path": "system_version"},
                {"type": "filter", "path": "embedding_model"},
            ]
        }))
    # Wait for the index to be queryable whether we just created it or it is (re)building — on Atlas
    # this takes ~20 s the first time. Agents still start if it never gets there, but say so loudly.
    for _ in range(120):
        idx = list(s.playbooks.list_search_indexes("playbook_vec"))
        if idx and idx[0].get("queryable"):
            break
        time.sleep(1)
    else:
        print("WARNING: vector index playbook_vec is not queryable yet; round-2 recall will miss until it is")


def reset() -> None:
    from chaos import faults
    faults.thaw_overdue(all_=True)
    faults.clear_planted_memory()
    w, s = world(), swarm()
    for n in ["systems", "system_state", "system_logs", "op_receipts", "chaos_events", "runs"]:
        w[n].delete_many({})
    for n in ["incidents", "tasks", "agents", "thoughts", "messages", "ledger", "playbooks", "approvals",
              "schema_samples", "schema_mappings",
              "upgrade_requests", "stats", "counters", "checkpoints", "checkpoint_writes", "spawn_requests",
              "custody"]:
        try:
            s[n].delete_many({})
        except OperationFailure:
            pass
    s.gardener_notes.delete_many({})
    s.gardener_requests.delete_many({})
    s.lessons.delete_many({})  # failure memory goes with the playbooks on a full reset (kept on next_round)


if __name__ == "__main__":
    ensure()
    if "--reset" in sys.argv:
        reset()
    print("setup_db ok")  # never print connection strings (including credential-bearing query options)
