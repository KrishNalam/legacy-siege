"""Chain of custody for leased work — built from MongoDB change-stream **pre-images** (MONGO_SPINE=1).

Every lease the Hive takes lives in one document (`tasks` for workers, `incidents` for scouts), and every
Beekeeper decision in `approvals`. `scripts/setup_db.ensure_spine` turns on
`changeStreamPreAndPostImages` for those collections, so each update event carries the document *as it
was* (`fullDocumentBeforeChange`) next to the stored post-image *as of that event* (`fullDocument:
"whenAvailable"`, never `updateLookup`, which could show a later state). This watcher turns
every ownership / generation / status change into one row of `agents.custody`:

    {doc_id, coll, incident, kind: claim|takeover|release|status|decision, from_owner, to_owner,
     gen_before, gen_after, status_before, status_after, lease_expired: bool, decided_by,
     pre_image: bool, cluster_time, ts, seq, prev_hash, hash}

Rows are hash-chained per document (`hash = sha256(prev_hash + row)`) with a unique `{doc_id, seq}` index, so
the trail for a task reads like a tamper-evident ledger: claimed by worker-mainframe-1 (gen 1) → SIGKILL →
taken over by nurse-1 (gen 2, the old lease had expired). The server-side `$match` only lets ownership and
status changes through (the fencing writes inside every step transaction don't wake it). The resume token is
persisted in `swarm.stats {_id: "custody_resume"}`. It runs as a thread inside the waggle (no new process).
"""
from __future__ import annotations

import hashlib
import json
import threading

from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

from common.db import col, now, swarm

TOKEN_ID = "custody_resume"
WATCH = ["tasks", "incidents", "approvals"]
PIPELINE = [{"$match": {
    "ns.coll": {"$in": WATCH},
    "$or": [
        {"operationType": "replace"},
        {"updateDescription.updatedFields.owner": {"$exists": True}},
        {"updateDescription.updatedFields.generation": {"$exists": True}},
        {"updateDescription.updatedFields.status": {"$exists": True}},
    ],
}}]


def _kind(coll: str, before: dict, after: dict) -> str | None:
    if coll == "approvals":
        if before.get("status") != after.get("status") and after.get("status") in ("approved", "denied"):
            return "decision"
        return None
    b_own, a_own = before.get("owner"), after.get("owner")
    if b_own != a_own or before.get("generation") != after.get("generation"):
        if a_own is None:
            return "release"
        if b_own is None or b_own == a_own:
            return "claim"
        return "takeover"
    if before.get("status") != after.get("status"):
        return "status"
    return None


def event_states(ev: dict) -> tuple[dict, dict, bool]:
    """(before, after, had_pre_image) as of THIS event, never a later state.

    ``updateLookup`` would return the document as it is when the event is *read*, so a takeover followed quickly
    by a release would look like a release and vanish from the trail. Instead: after = the stored post-image
    (``fullDocument: "whenAvailable"``, enabled by ``changeStreamPreAndPostImages``), else pre-image +
    ``updateDescription``; replace events carry their own replacement document."""
    pre = ev.get("fullDocumentBeforeChange")
    desc = ev.get("updateDescription") or {}
    upd, removed = desc.get("updatedFields") or {}, desc.get("removedFields") or []
    post = ev.get("fullDocument")
    if post is None:
        post = {k: v for k, v in (pre or {}).items() if k not in removed}
        post.update({k: v for k, v in upd.items() if "." not in k})  # tracked fields are all top-level
    if pre is None:  # no images: only the fields this update touched are known for sure
        return {}, post, False
    return pre, post, True


def row_from_event(ev: dict) -> dict | None:
    """Change event -> custody row (without seq/hash), or None when nothing custody-relevant changed."""
    coll = ev["ns"]["coll"]
    before, after, had_pre = event_states(ev)
    kind = _kind(coll, before, after)
    if kind is None:
        return None
    ct = ev.get("clusterTime")
    lease_b = before.get("lease_until")
    wall = ev.get("wallTime") or now()
    return {
        "doc_id": ev["documentKey"]["_id"], "coll": coll,
        "incident": after.get("incident") if coll != "incidents" else ev["documentKey"]["_id"],
        "kind": kind, "from_owner": before.get("owner"), "to_owner": after.get("owner"),
        "gen_before": before.get("generation"), "gen_after": after.get("generation"),
        "status_before": before.get("status"), "status_after": after.get("status"),
        "lease_expired": bool(lease_b is not None and kind == "takeover" and lease_b < wall),
        "decided_by": after.get("decided_by") if coll == "approvals" else None,
        "pre_image": had_pre,
        "cluster_time": ct, "ts": now(),
    }


def _digest(prev_hash: str, row: dict) -> str:
    body = {k: row.get(k) for k in ("doc_id", "coll", "kind", "from_owner", "to_owner", "gen_before", "gen_after",
                                     "status_before", "status_after", "seq")}
    return hashlib.sha256((prev_hash + json.dumps(body, sort_keys=True, default=str)).encode()).hexdigest()


def append(row: dict) -> dict:
    """Append to the document's hash chain. The unique {doc_id, seq} index makes a concurrent double-append
    (e.g. two waggles after a respawn) fail instead of forking the chain; we retry on the new tip."""
    for _ in range(5):
        tip = col("custody").find_one({"doc_id": row["doc_id"]}, sort=[("seq", -1)], projection={"seq": 1, "hash": 1})
        row["seq"] = (tip or {}).get("seq", 0) + 1
        row["prev_hash"] = (tip or {}).get("hash", "")
        row["hash"] = _digest(row["prev_hash"], row)
        row.pop("_id", None)
        try:
            col("custody").insert_one(row)
            return row
        except DuplicateKeyError:
            continue
    return row


def verify_chain(doc_id) -> bool:
    """Recompute every hash of one document's custody chain (the war room / tests can call this)."""
    prev = ""
    for r in col("custody").find({"doc_id": doc_id}).sort("seq", 1):
        if r.get("prev_hash") != prev or _digest(prev, r) != r.get("hash"):
            return False
        prev = r["hash"]
    return True


def narrate(row: dict) -> str | None:
    if row["kind"] != "takeover":
        return None
    why = "its lease had expired" if row["lease_expired"] else "lease moved"
    return (f"Custody {row['doc_id']}: {row['from_owner']} (gen {row['gen_before']}) → {row['to_owner']} "
            f"(gen {row['gen_after']}), {why} · MongoDB pre-image #{row['seq']}")


def run(stop: threading.Event, on_takeover=None, max_await_ms: int = 500) -> None:
    """Watch until ``stop`` is set. ``on_takeover(text, row)`` is called for takeovers (the waggle narrates)."""
    while not stop.is_set():
        token = (col("stats").find_one({"_id": TOKEN_ID}) or {}).get("token")
        try:
            with swarm().watch(PIPELINE, full_document="whenAvailable", full_document_before_change="whenAvailable",
                               resume_after=token, max_await_time_ms=max_await_ms) as stream:
                while not stop.is_set():
                    ev = stream.try_next()
                    if ev is None:
                        continue
                    row = row_from_event(ev)
                    if row is not None:
                        append(row)
                        text = narrate(row)
                        if text and on_takeover:
                            on_takeover(text, row)
                    col("stats").update_one({"_id": TOKEN_ID}, {"$set": {"token": stream.resume_token}}, upsert=True)
        except OperationFailure:
            col("stats").delete_one({"_id": TOKEN_ID})  # token too old / history lost: start fresh
        except PyMongoError:
            stop.wait(1.0)


def start(stop: threading.Event, on_takeover=None) -> threading.Thread:
    t = threading.Thread(target=run, args=(stop, on_takeover), name="custody", daemon=True)
    t.start()
    return t
