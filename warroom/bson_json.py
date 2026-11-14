"""BSON -> JSON-safe Python values for the war room.

Change-stream documents contain datetimes, ObjectIds, Binary blobs (LangGraph checkpoints)
and 1024-float embeddings. The browser needs none of that raw: datetimes become ISO strings,
ObjectIds become hex, binaries become a size marker, and every ``embedding`` array is
replaced with ``{dims, model}`` so a playbook never ships 8 KB of floats to a projector.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from bson import Binary, Decimal128, Int64, ObjectId, Timestamp
from bson.regex import Regex

# Checkpoint docs (LangGraph MongoDBSaver) are mostly msgpack blobs; the viz only needs ids.
CHECKPOINT_FIELDS = ("thread_id", "checkpoint_ns", "checkpoint_id", "parent_checkpoint_id", "type", "task_id",
                     "idx", "channel")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def to_json(value: Any) -> Any:
    """Recursively convert a BSON value into something ``json.dumps`` accepts."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "embedding" and isinstance(v, list):
                out[k] = {"dims": len(v), "model": value.get("embedding_model"), "stripped": True}
            else:
                out[k] = to_json(v)
        return out
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, Int64):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (Binary, bytes)):
        return {"$binary": f"{len(value)} bytes"}
    if isinstance(value, Decimal128):
        return str(value.to_decimal())
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Timestamp):
        return {"t": value.time, "i": value.inc}
    if isinstance(value, Regex):
        return f"/{value.pattern}/{value.flags}"
    return str(value)


def slim(coll: str, doc: dict | None) -> dict | None:
    """Collection-specific trimming before a doc goes over the WebSocket."""
    if doc is None:
        return None
    if coll in ("checkpoints", "checkpoint_writes"):
        doc = {k: doc[k] for k in ("_id", *CHECKPOINT_FIELDS) if k in doc}
    return to_json(doc)

