"""MONGO_SPINE (W25): change-stream pre-images -> hash-chained lease custody; validators on tasks/approvals.
Protects the kill + nurse takeover beat's audit trail and the switch's on/off contract."""
from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from pymongo.errors import WriteError

from swarm.tests.conftest import reset_runtime, wait_for  # noqa: F401  (sets test env first)


@pytest.fixture()
def spine(db):
    from common.db import swarm
    from scripts import setup_db
    reset_runtime()
    swarm().custody.delete_many({})
    swarm().stats.delete_many({"_id": "custody_resume"})
    setup_db.ensure_spine(swarm())
    yield swarm()
    # leave the shared test DB as the other tests expect it (switch off)
    for name in setup_db.SPINE_VALIDATORS:
        swarm().command("collMod", name, validator={}, validationLevel="off")
    for name in setup_db.PRE_IMAGE_COLLECTIONS:
        swarm().command("collMod", name, changeStreamPreAndPostImages={"enabled": False})


def test_switch_is_off_by_default():
    from common import config
    assert config.MONGO_SPINE is False


def test_takeover_is_recorded_from_pre_images_and_chain_verifies(spine):
    from common.db import col, now
    from swarm import custody, leases
    col("tasks").insert_one({"_id": "INC-0001-t1", "incident": "INC-0001", "kind": "fix", "specialty": "mainframe",
                             "step_ids": [], "status": "open", "owner": None, "lease_until": None, "generation": 0,
                             "ts": now()})
    stop, said = threading.Event(), []
    t = custody.start(stop, lambda text, row: said.append(text))
    try:
        time.sleep(1.0)  # let the stream open before the writes
        won = leases.claim(col("tasks"), {"_id": "INC-0001-t1"}, "worker-mainframe-1",
                           extra_set={"status": "claimed"})
        assert won["generation"] == 1
        col("tasks").update_one({"_id": "INC-0001-t1"}, {"$set": {"lease_until": now() - timedelta(seconds=5)}})
        assert leases.takeover(col("tasks"), "INC-0001-t1", "worker-mainframe-1", 1, "nurse-1")
        rows = wait_for(lambda: (r := list(col("custody").find({"doc_id": "INC-0001-t1"}).sort("seq", 1)))
                        and len(r) >= 2 and r, 10, what="custody rows")
    finally:
        stop.set()
        t.join(3)
    claim, take = rows[0], rows[1]
    assert (claim["kind"], claim["to_owner"], claim["gen_after"]) == ("claim", "worker-mainframe-1", 1)
    assert (take["kind"], take["from_owner"], take["to_owner"]) == ("takeover", "worker-mainframe-1", "nurse-1")
    assert take["gen_before"] == 1 and take["gen_after"] == 2 and take["lease_expired"] and take["pre_image"]
    assert take["prev_hash"] == claim["hash"] and custody.verify_chain("INC-0001-t1")
    assert any("worker-mainframe-1 (gen 1) → nurse-1 (gen 2)" in s for s in said)
    col("custody").update_one({"_id": take["_id"]}, {"$set": {"to_owner": "mallory"}})
    assert not custody.verify_chain("INC-0001-t1")  # tamper-evident


def test_validators_reject_bad_lease_and_undecided_approval(spine):
    from common.db import col, now
    with pytest.raises(WriteError):
        col("tasks").insert_one({"_id": "bad", "incident": "INC-1", "kind": "fix", "specialty": "unix",
                                 "status": "stolen", "generation": 0})
    with pytest.raises(WriteError):
        col("tasks").insert_one({"_id": "bad2", "incident": "INC-1", "kind": "fix", "specialty": "unix",
                                 "status": "open", "generation": "one"})
    col("approvals").insert_one({"_id": "APR-1", "kind": "step", "status": "pending", "ts": now()})
    with pytest.raises(WriteError):  # a decision must say who decided and when
        col("approvals").update_one({"_id": "APR-1"}, {"$set": {"status": "approved"}})
    col("approvals").update_one({"_id": "APR-1"}, {"$set": {"status": "approved", "decided_by": "beekeeper",
                                                             "decided_at": now()}})


def test_takeover_then_quick_release_keeps_both(spine):
    """RV1: the after-state must be the event's own (post-image / pre-image + updateDescription), not a later
    lookup. Events are replayed from a token taken BEFORE the writes, so every lookup would see the release."""
    from common.db import col, now, swarm
    from swarm import custody, leases
    col("tasks").insert_one({"_id": "INC-0002-t1", "incident": "INC-0002", "kind": "fix", "specialty": "unix",
                             "step_ids": [], "status": "claimed", "owner": "worker-unix-1",
                             "lease_until": now() - timedelta(seconds=5), "generation": 1, "ts": now()})
    with swarm().watch(custody.PIPELINE, max_await_time_ms=100) as cs:
        cs.try_next()
        token = cs.resume_token
    col("stats").update_one({"_id": custody.TOKEN_ID}, {"$set": {"token": token}}, upsert=True)
    won = leases.takeover(col("tasks"), "INC-0002-t1", "worker-unix-1", 1, "nurse-1")
    assert leases.release(col("tasks"), "INC-0002-t1", "nurse-1", won["generation"], {"status": "done"})
    stop = threading.Event()
    t = custody.start(stop)
    try:
        rows = wait_for(lambda: (r := list(col("custody").find({"doc_id": "INC-0002-t1"}).sort("seq", 1)))
                        and len(r) >= 2 and r, 10, what="custody rows")
    finally:
        stop.set()
        t.join(3)
    assert [(r["kind"], r["from_owner"], r["to_owner"]) for r in rows] == [
        ("takeover", "worker-unix-1", "nurse-1"), ("release", "nurse-1", None)]
    assert rows[0]["gen_after"] == 2 and rows[0]["status_after"] == "claimed" and rows[1]["status_after"] == "done"
    assert custody.verify_chain("INC-0002-t1")


def test_event_states_without_post_image():
    """Fallback path: no stored post-image -> pre-image + updateDescription (dotted array paths ignored)."""
    from swarm import custody
    ev = {"fullDocumentBeforeChange": {"_id": "t", "owner": "w", "generation": 1, "status": "claimed", "x": 1},
          "updateDescription": {"updatedFields": {"owner": "n", "generation": 2, "steps.0.status": "sent"},
                                "removedFields": ["x"]}}
    before, after, had_pre = custody.event_states(ev)
    assert had_pre and before["owner"] == "w" and after == {"_id": "t", "owner": "n", "generation": 2,
                                                            "status": "claimed"}
