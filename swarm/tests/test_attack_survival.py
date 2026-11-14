"""W28 G7 + G8: the Hive finishes the job when the attack hits its infrastructure, not a system.

G7  the Red Assassin kills the Guard while a step is awaiting read-back: the supervisor respawns guard-1, which
    re-judges the pending attempt from MongoDB (the Guard is stateless); nothing is applied twice.
G8  the estate's API drops out mid-fix (503 for a few seconds, then one op whose reply is lost after it applied):
    transport retries reuse the SAME action_id (stored receipt), a failed attempt is read back and retried, the
    incident resolves and every op took effect exactly once."""
from __future__ import annotations

import os
import signal
import time

import pytest

from swarm.tests.conftest import reset_runtime, run_until_resolved, start_swarm, wait_for

ONCE = {"mainframe.quarantine_record": 1, "mainframe.restart_batch": 1, "unix.rerun_transfer": 1,
        "portal.refresh_cache": 1}


@pytest.fixture(scope="module")
def hive(db, fake):
    reset_runtime()
    fake.post("/_admin/reset")
    s = start_swarm({"STEP_HOLD_S": "1.0", "NURSE_RESPAWN_S": "2"}, log_name="swarm_survival")
    yield s
    s.stop()


@pytest.fixture
def fresh(hive, fake):
    from common.db import col
    for c in ("incidents", "tasks", "ledger", "approvals", "playbooks", "lessons"):
        col(c).delete_many({})
    fake.post("/_admin/reset")
    yield
    fake.post("/_admin/outage", json={"seconds": 0, "lose_replies": 0})


def test_guard_killed_mid_verify_is_respawned_and_rejudges(fresh, fake):
    from common.db import col
    col("stats").update_one({"_id": "current"}, {"$set": {"run_id": "RUN-G7", "round": 1}}, upsert=True)
    fake.post("/mainframe/_chaos/batch_abort", json={"params": {}})
    wait_for(lambda: col("agents").find_one({"role": "worker", "phase": {"$in": ["step_sent", "awaiting_verify"]}}),
             60, 0.05, "a step awaiting the Guard")
    guard = col("agents").find_one({"_id": "guard-1"})
    os.kill(guard["pid"], signal.SIGKILL)
    killed_at = time.time()

    inc = run_until_resolved(1, timeout=120)
    assert all(s["status"] == "verified" and s.get("evidence") for s in inc["steps"])
    back = col("agents").find_one({"_id": "guard-1"})
    assert back["pid"] != guard["pid"] and back["status"] != "dead"       # respawned in its own role
    late = col("ledger").count_documents({"incident": inc["_id"], "status": "verified",
                                          "updated_at": {"$gte": inc["opened_at"]}})
    assert late == len(inc["steps"])
    assert inc["closed_at"].timestamp() > killed_at                       # finished after the kill, not before
    assert fake.get("/_fake/receipts").json()["applied"] == ONCE          # no op applied twice


def test_systems_outage_mid_fix_retries_same_action_and_finishes(fresh, fake):
    from common.db import col
    col("stats").update_one({"_id": "current"}, {"$set": {"run_id": "RUN-G8", "round": 1}}, upsert=True)
    fake.post("/mainframe/_chaos/batch_abort", json={"params": {}})
    wait_for(lambda: col("incidents").find_one({"active": True, "steps.0": {"$exists": True}}), 60, 0.05,
             "a fix plan")
    fake.post("/_admin/outage", json={"seconds": 2.5, "lose_replies": 1})

    inc = run_until_resolved(1, timeout=150)
    assert all(s["status"] == "verified" for s in inc["steps"])
    rec = fake.get("/_fake/receipts").json()
    assert rec["applied"] == ONCE                                          # exactly once despite the outage
    assert any(n > 1 for n in rec["posts"].values())                       # someone did retry the same action_id
