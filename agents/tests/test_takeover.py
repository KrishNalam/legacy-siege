"""(4) SIGKILL a real operator PROCESS mid-step -> Reaper marks it dead and releases its lease -> a Recovery
claims the task (generation+1), loads the LangGraph checkpoint from MongoDB, reconciles the 'sent' step
against /state, resends with the SAME action_id, and finishes. No op is applied twice (fake receipts);
the handoff is recorded; the supervisor refills the bench with a fresh recovery."""
from __future__ import annotations

import os
import signal

import pytest

from agents.tests.conftest import reset_runtime, run_until_resolved, start_swarm, wait_for


@pytest.fixture(scope="module")
def hive(db, fake):
    reset_runtime()
    fake.post("/_admin/reset")
    # STEP_HOLD_S widens the window between "step marked sent" and the POST: the judge's kill button
    s = start_swarm({"STEP_HOLD_S": "1.5"}, log_name="swarm_takeover")
    yield s
    s.stop()


def test_sigkill_operator_mid_step_recovery_resumes_without_duplicates(hive, fake):
    from common.db import col

    col("stats").update_one({"_id": "current"}, {"$set": {"run_id": "RUN-K", "round": 1}}, upsert=True)
    fake.post("/mainframe/_chaos/batch_abort", json={"params": {}})

    # wait for an operator to be mid-step (exactly what the Chaos kill selector waits for)
    victim = wait_for(lambda: col("agents").find_one({"role": "operator", "phase": "step_sent"}), 60, 0.05,
                      "an operator in phase step_sent")
    inc0 = col("incidents").find_one({"active": True})
    sent = [s for s in inc0["steps"] if s["status"] == "sent"]
    assert sent, "a step must be in 'sent' when the operator is killed"
    killed_step = sent[0]
    os.kill(victim["pid"], signal.SIGKILL)

    inc = run_until_resolved(1, timeout=120)

    # the reaper declared the victim dead; a recovery took over from the checkpoint
    assert col("agents").find_one({"_id": victim["_id"]})["status"] == "dead"
    hs = [h for h in inc["handoffs"] if h["from"] == victim["_id"]]
    assert hs, inc["handoffs"]
    h = hs[0]
    assert h["to"].startswith("recovery-") and h["kind"] == "task" and h["checkpoint_id"]
    assert h["work"] == killed_step["task_id"] and h["takeover_s"] > 0
    assert any(r["step_id"] == killed_step["step_id"] for r in h["reconciled"])
    task = col("tasks").find_one({"_id": killed_step["task_id"]})
    assert task["generation"] >= 2 and task["status"] == "done"

    # exactly-once: the killed attempt kept its action_id; every op took effect exactly once
    step = next(s for s in inc["steps"] if s["step_id"] == killed_step["step_id"])
    assert step["status"] == "verified" and step["attempts"] == [killed_step["action_id"]]
    rec = fake.get("/_fake/receipts").json()
    assert set(rec["applied"].values()) == {1}, rec["applied"]
    assert rec["applied"] == {"mainframe.quarantine_record": 1, "mainframe.restart_batch": 1,
                              "unix.rerun_transfer": 1, "portal.refresh_cache": 1}
    assert col("ledger").count_documents({"action_id": killed_step["action_id"]}) == 1
    assert all(s["status"] == "verified" for s in inc["steps"])
    assert inc["metrics"]["takeover_s"] > 0

    # narration: recovery handoff thought + reporter handoff note; bench refilled by the supervisor
    assert col("thoughts").find_one({"kind": "handoff", "agent": h["to"], "text": {"$regex": "checkpoint"}})
    assert wait_for(lambda: col("messages").find_one({"from": "reporter-1", "kind": "handoff",
                                                       "body": {"$regex": f"{victim['_id']} died"}}), 10)
    wait_for(lambda: col("agents").find_one({"role": "recovery", "standby_for": victim["_id"],
                                             "status": {"$ne": "dead"}}), 20, 0.2, "replacement recovery")


def test_sigkill_investigator_while_awaiting_approval_recovery_resumes_investigator_graph(hive, fake):
    """Kill the investigator while its graph is parked on a LangGraph interrupt (approval); a recovery resumes the
    investigator thread from MongoDB, picks up the decision and finishes the incident."""
    from common.db import col

    fake.post("/_admin/reset")
    fake.post("/mainframe/_chaos/batch_abort", json={"params": {}})
    inc0 = wait_for(lambda: col("incidents").find_one({"active": True, "status": "awaiting_approval"}), 90, 0.1,
                    "incident awaiting approval")
    investigator = col("agents").find_one({"_id": inc0["owner"]})
    assert investigator["role"] == "investigator"
    # The incident status is written before LangGraph persists the interrupt. Kill only
    # once the durable checkpoint confirms the exact resume point asserted below.
    from types import SimpleNamespace
    from agents.roles.investigator import build_graph, investigator_config
    graph = build_graph(SimpleNamespace(systems=None))
    config = investigator_config(inc0["_id"], investigator["_id"], inc0["generation"])
    wait_for(lambda: any(t.interrupts for t in graph.get_state(config).tasks),
             30, .1, "durable approval interrupt")
    os.kill(investigator["pid"], signal.SIGKILL)

    inc = run_until_resolved(2, timeout=120)
    assert inc["_id"] == inc0["_id"]
    h = next(h for h in inc["handoffs"] if h["from"] == investigator["_id"])
    assert h["kind"] == "incident" and h["to"].startswith("recovery-") and h["checkpoint_id"]
    assert h["resume_at"] == ["approve"]           # parked exactly on the interrupt
    assert inc["owner"] == h["to"] and inc["generation"] >= 2
    assert all(s["status"] == "verified" for s in inc["steps"])
    assert inc["metrics"]["approval_wait_s"] > 0 and inc["metrics"]["takeover_s"] > 0


def test_coordinator_failover(hive):
    from common.db import col

    q1 = col("agents").find_one({"_id": "coordinator-1"})
    assert col("stats").find_one({"_id": "coordinator_lease"})["owner"] == "coordinator-1"
    os.kill(q1["pid"], signal.SIGKILL)
    wait_for(lambda: col("stats").find_one({"_id": "coordinator_lease", "owner": "coordinator-2"}), 20, 0.1, "coordinator-2 to take over")
    assert wait_for(lambda: col("thoughts").find_one({"agent": "coordinator-2", "kind": "handoff"}), 5)
    assert wait_for(lambda: col("messages").find_one({"from": "reporter-1", "body": {"$regex": "coordinator-2 now holds"}}), 5)
