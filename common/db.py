"""MongoDB access — the spine. Every process gets state from here and nowhere else."""
from __future__ import annotations

import signal
import time
from datetime import datetime, timezone
from functools import lru_cache

from pymongo import MongoClient, ReturnDocument  # noqa: F401  (re-export)
from pymongo.collection import Collection
from pymongo.database import Database

from . import config

WORLD_COLLECTIONS = {"systems", "system_state", "system_logs", "op_receipts", "chaos_scenarios", "chaos_events",
                     "runs"}
SWARM_COLLECTIONS = {
    "incidents", "agents", "thoughts", "messages", "ledger", "playbooks",
    "approvals", "upgrade_requests", "stats", "counters", "tasks", "checkpoints", "checkpoint_writes",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


@lru_cache(maxsize=1)
def client() -> MongoClient:
    return MongoClient(config.MONGODB_URI, tz_aware=True, serverSelectionTimeoutMS=5000, appname="legacy-siege")


def world() -> Database:
    return client()[config.WORLD_DB]


def swarm() -> Database:
    return client()[config.SWARM_DB]


def col(name: str) -> Collection:
    """Collection by bare name; routes to the right DB."""
    if name in WORLD_COLLECTIONS:
        return world()[name]
    return swarm()[name]


def txn(fn):
    """Run ``fn(session)`` as ONE multi-document transaction and return its result.

    ``with_transaction`` retries the whole callback on TransientTransactionError (e.g. a write
    conflict with a concurrent agent) and retries the commit on UnknownTransactionCommitResult —
    both happen on a real Atlas replica set, so every transaction in the project goes through here.
    ``fn`` must therefore be safe to re-run (only DB writes through ``session``).
    """
    with client().start_session() as s:
        return s.with_transaction(fn)


def next_id(prefix: str, width: int = 4, db: Database | None = None) -> str:
    """Atomic human-readable ids: INC-0001, APR-0001, UPG-0001. Counters live in the caller's own DB
    (default swarm; Red passes ``db=world()`` so it never writes Blue's database)."""
    doc = (db if db is not None else swarm())["counters"].find_one_and_update(
        {"_id": prefix}, {"$inc": {"n": 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return f"{prefix}-{doc['n']:0{width}d}"


# ---------------------------------------------------------------- network partition (chaos)
class PartitionError(RuntimeError):
    """Raised while this process is 'partitioned' from MongoDB by the Chaos Monkey."""


_partition_until = 0.0


def install_partition_handler() -> None:
    """Agents call this at start. Chaos sends SIGUSR1 → no DB access for PARTITION_S seconds."""

    def _handler(signum, frame):  # noqa: ARG001
        global _partition_until
        _partition_until = time.monotonic() + config.PARTITION_S

    signal.signal(signal.SIGUSR1, _handler)


def partitioned() -> bool:
    return time.monotonic() < _partition_until


def check() -> None:
    """Call before any DB-dependent action in agent loops."""
    if partitioned():
        raise PartitionError("cannot reach MongoDB (chaos partition)")
