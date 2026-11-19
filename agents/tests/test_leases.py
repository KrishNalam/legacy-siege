"""(1) Lease claims are exclusive under concurrency; fencing rejects stale-generation writes."""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from agents.tests.conftest import reset_runtime  # noqa: F401  (sets test env first)


@pytest.fixture()
def clean(db):
    reset_runtime()
    yield db


def _race(n: int, fn) -> list:
    barrier, out = threading.Barrier(n), [None] * n

    def go(i):
        barrier.wait()
        out[i] = fn(i)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def test_claim_is_exclusive_under_concurrency(clean):
    from common.db import col
    from agents import leases

    for k in range(5):  # repeat to give races a chance
        col("tasks").insert_one({"_id": f"T{k}", "status": "open", "owner": None, "lease_until": None, "generation": 0})
        won = _race(16, lambda i, k=k: leases.claim(col("tasks"), {"_id": f"T{k}"}, f"bee-{i}"))
        winners = [w for w in won if w]
        assert len(winners) == 1
        assert col("tasks").find_one({"_id": f"T{k}"})["generation"] == 1


def test_takeover_of_expired_lease_has_one_winner(clean):
    from common.db import col
    from agents import leases

    col("tasks").insert_one({"_id": "T", "status": "claimed", "owner": "dead-bee", "lease_until": leases.EPOCH,
                             "generation": 4})
    won = _race(8, lambda i: leases.takeover(col("tasks"), "T", "dead-bee", 4, f"nurse-{i}"))
    assert len([w for w in won if w]) == 1
    assert col("tasks").find_one({"_id": "T"})["generation"] == 5


def test_live_lease_cannot_be_claimed(clean):
    from common.db import col
    from agents import leases

    col("tasks").insert_one({"_id": "T", "owner": None, "lease_until": None, "generation": 0})
    assert leases.claim(col("tasks"), {"_id": "T"}, "a")
    assert leases.claim(col("tasks"), {"_id": "T"}, "b") is None


def test_fencing_rejects_stale_generation(clean):
    from common.db import col, now
    from agents import leases, steps

    col("incidents").insert_one({"_id": "INC-T", "status": "fixing", "system": "mainframe", "opened_at": now(),
                                 "active": True, "steps": [{"step_id": "INC-T-s1", "n": 1, "system": "mainframe",
                                                            "op": "quarantine_record", "params": {"record_id": "R1"},
                                                            "status": "planned", "attempts": []}]})
    col("tasks").insert_one({"_id": "INC-T-f1", "incident": "INC-T", "step_ids": ["INC-T-s1"], "status": "open",
                             "owner": None, "lease_until": None, "generation": 0})
    a = leases.claim(col("tasks"), {"_id": "INC-T-f1"}, "worker-a")
    assert a["generation"] == 1
    # worker-a stalls; its lease expires; a nurse takes over with generation 2
    col("tasks").update_one({"_id": "INC-T-f1"}, {"$set": {"lease_until": now() - timedelta(seconds=1)}})
    b = leases.takeover(col("tasks"), "INC-T-f1", "worker-a", 1, "nurse-1")
    assert b["generation"] == 2

    assert leases.renew(col("tasks"), "INC-T-f1", "worker-a", 1) is False
    step = steps.get_step("INC-T", "INC-T-s1")
    with pytest.raises(leases.Fenced):
        steps.mark_sent(a, "worker-a", 1, step, "INC-T-s1-a1")
    # the stale write left NO trace: step untouched, no ledger receipt (the transaction aborted)
    assert steps.get_step("INC-T", "INC-T-s1")["status"] == "planned"
    assert col("ledger").count_documents({}) == 0

    steps.mark_sent(b, "nurse-1", 2, step, "INC-T-s1-a1")
    s = steps.get_step("INC-T", "INC-T-s1")
    assert s["status"] == "sent" and s["agent"] == "nurse-1" and s["attempts"] == ["INC-T-s1-a1"]
    assert col("ledger").find_one({"action_id": "INC-T-s1-a1"})["status"] == "sent"
    assert leases.release(col("tasks"), "INC-T-f1", "worker-a", 1) is False
    assert leases.release(col("tasks"), "INC-T-f1", "nurse-1", 2) is True


def test_only_matching_attempt_can_be_judged(clean):
    from common.db import col, now
    from agents import leases, steps

    col("incidents").insert_one({"_id": "INC-J", "status": "fixing", "system": "unix", "opened_at": now(),
                                 "active": True, "steps": [{"step_id": "INC-J-s1", "n": 1, "system": "unix",
                                                            "op": "purge_spool", "params": {}, "status": "planned",
                                                            "attempts": []}]})
    col("tasks").insert_one({"_id": "INC-J-f1", "incident": "INC-J", "step_ids": ["INC-J-s1"], "owner": None,
                             "lease_until": None, "generation": 0})
    t = leases.claim(col("tasks"), {"_id": "INC-J-f1"}, "w")
    step = steps.get_step("INC-J", "INC-J-s1")
    steps.mark_sent(t, "w", 1, step, "INC-J-s1-a1")
    step = steps.get_step("INC-J", "INC-J-s1")
    assert steps.verify("INC-J", step, "INC-J-s1-a0", "x", "guard-1") is False   # wrong attempt
    assert steps.reject("INC-J", step, "INC-J-s1-a1", "ACK ≠ APPLIED", "guard-1") is True
    assert steps.verify("INC-J", step, "INC-J-s1-a1", "late", "guard-1") is False  # no longer 'sent'
    # the $jsonSchema validator refuses a verified step without evidence
    from pymongo.errors import WriteError
    with pytest.raises(WriteError):
        col("incidents").update_one({"_id": "INC-J"}, {"$set": {"steps.0.status": "verified", "steps.0.evidence": ""}})
