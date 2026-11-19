"""Leases with fencing tokens — the heart of "work is held, not owned".

* claim:   one atomic findOneAndUpdate. Matches only if nobody holds the lease (owner null) or the
           holder's lease_until has passed; bumps `generation` (the fencing token).
* renew / release: conditional on {owner, generation}; a replaced holder matches nothing.
* fence:   inside a transaction, a no-op-ish write on the lease doc conditional on {owner, generation}.
           If a recovery has claimed the doc meanwhile, the write matches 0 docs (or write-conflicts) and the
           whole transaction aborts — so a stale holder can never write steps or ledger rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.client_session import ClientSession
from pymongo.collection import Collection

from common import config
from . import settings
from common.db import now

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class LeaseLost(RuntimeError):
    """This process no longer holds the lease; it must stop acting on that work item."""


class Fenced(LeaseLost):
    """A write was rejected because the fencing token (owner, generation) no longer matches."""


def until(lease_s: float | None = None) -> datetime:
    return now() + timedelta(seconds=lease_s if lease_s is not None else config.LEASE_S)


def claim(coll: Collection, query: dict, owner: str, lease_s: float | None = None,
          extra_set: dict | None = None, sort: list | None = None) -> dict | None:
    """Atomically take the lease on the first doc matching `query` that is free or expired."""
    t = now()
    filt = {**query, "$or": [{"owner": None}, {"lease_until": {"$lt": t}}]}
    return coll.find_one_and_update(
        filt,
        {"$set": {"owner": owner, "lease_until": until(lease_s), **(extra_set or {})}, "$inc": {"generation": 1}},
        sort=sort, return_document=ReturnDocument.AFTER,
    )


def takeover(coll: Collection, _id, prev_owner: str | None, prev_gen: int, owner: str,
             extra_set: dict | None = None) -> dict | None:
    """Claim an *expired* lease from a known previous holder (recovery takeover). Exactly one recovery wins."""
    return coll.find_one_and_update(
        {"_id": _id, "owner": prev_owner, "generation": prev_gen, "lease_until": {"$lt": now()}},
        {"$set": {"owner": owner, "lease_until": until(), **(extra_set or {})}, "$inc": {"generation": 1}},
        return_document=ReturnDocument.AFTER,
    )


def renew(coll: Collection, _id, owner: str, generation: int, lease_s: float | None = None) -> bool:
    filt = {"_id": _id, "owner": owner, "generation": generation}
    if settings.lease_deadline_enabled():
        filt["lease_until"] = {"$gt": now()}
    r = coll.update_one(filt, {"$set": {"lease_until": until(lease_s)}})
    return r.matched_count == 1


def release(coll: Collection, _id, owner: str, generation: int, extra_set: dict | None = None,
            clear_owner: bool = True) -> bool:
    s = {"lease_until": EPOCH, **(extra_set or {})}
    if clear_owner:
        s["owner"] = None
    r = coll.update_one({"_id": _id, "owner": owner, "generation": generation}, {"$set": s})
    return r.matched_count == 1


def fence(coll: Collection, _id, owner: str, generation: int, session: ClientSession) -> None:
    """Call first inside a transaction. Raises Fenced if (owner, generation) is no longer current."""
    filt = {"_id": _id, "owner": owner, "generation": generation}
    if settings.lease_deadline_enabled():
        filt["lease_until"] = {"$gt": now()}
    r = coll.update_one(filt, {"$set": {"fenced_at": now()}}, session=session)
    if r.matched_count != 1:
        raise Fenced(f"{coll.name}/{_id}: lease no longer held by {owner} gen {generation}")
