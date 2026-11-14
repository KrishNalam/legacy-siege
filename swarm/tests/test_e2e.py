"""(2) Round 1 end-to-end against the fake estate, with the real supervisor + 12 agent processes:
batch_abort cascade -> ONE coalesced incident -> diagnosis walk portal→unix→mainframe -> approval for
restart_batch -> lying_success caught by the Guard (reject + new attempt) -> resolved -> playbook.
(3) Round 2, same fault: $vectorSearch recall hit is used, fewer hops; the recurrence files an upgrade
request; a version bump marks playbooks stale."""
from __future__ import annotations

import pytest

from swarm.tests.conftest import reset_runtime, run_until_resolved, start_swarm, wait_for


@pytest.fixture(scope="module")
def hive(db, fake):
    reset_runtime()
    fake.post("/_admin/reset")
    s = start_swarm(log_name="swarm_e2e")
    yield s
    s.stop()


def test_round1_cascade_lie_approval_resolve(hive, fake):
    from common.db import col

    col("stats").update_one({"_id": "current"}, {"$set": {"run_id": "RUN-T1", "round": 1}}, upsert=True)
    fake.post("/mainframe/_chaos/lying_success", json={"params": {"count": 1}})
    fake.post("/mainframe/_chaos/batch_abort", json={"params": {}})

    inc = run_until_resolved(1, timeout=120)

    # exactly one incident for the whole cascade, coalesced across unix and portal
    assert col("incidents").count_documents({}) == 1
    assert {"unix", "portal"} <= set(inc["affected"])
    assert inc["correlation_key"] == "chain:portal" and inc["system"] == "portal"
    assert inc["run_id"] == "RUN-T1" and inc["round"] == 1

    # the walk: symptom far from cause
    assert inc["diagnosis_path"] == ["portal", "unix", "mainframe"]
    assert inc["root_cause_system"] == "mainframe" and inc["fault_class"] == "batch_abort"
    assert "REC-00042" in inc["root_cause"]
    assert inc["recall"]["used"] is False and inc["recall"]["filter"]["stale"] is False

    ops = [s["op"] for s in inc["steps"]]
    assert ops == ["quarantine_record", "restart_batch", "rerun_transfer", "refresh_cache"]
    assert all(s["status"] == "verified" and s["evidence"] for s in inc["steps"])

    # the lie: first mainframe op ACKed applied:true but did nothing -> Guard rejected -> new attempt
    s1 = inc["steps"][0]
    assert s1["attempts"] == [f"{s1['step_id']}-a1", f"{s1['step_id']}-a2"]
    assert col("ledger").find_one({"action_id": s1["attempts"][0]})["status"] == "failed"
    assert col("thoughts").find_one({"kind": "reject", "text": {"$regex": "ACK ≠ APPLIED"}})
    assert "quarantined=['REC-00042']" in s1["evidence"]
    rec = fake.get("/_fake/receipts").json()
    assert rec["receipts"][s1["attempts"][0]]["lied"] is True
    assert rec["applied"]["mainframe.quarantine_record"] == 1

    # approval gated the destructive step, and was granted before it was sent
    s2 = inc["steps"][1]
    apr = col("approvals").find_one({"_id": s2["approval_id"]})
    assert apr["status"] == "approved" and apr["relayed_by"] in ("queen-1", "queen-2")
    assert apr["decided_at"] <= col("ledger").find_one({"action_id": s2["action_id"]})["ts"]
    assert col("ledger").find_one({"action_id": s2["action_id"]})["approver"] == "test-beekeeper"

    # metrics + learning
    m = inc["metrics"]
    assert m["hops"] == 3 and m["steps"] == 4 and m["attempts"] == 5 and m["ttr_s"] > 0
    pb = col("playbooks").find_one({"incident": inc["_id"]})
    assert pb and pb["system"] == "mainframe" and pb["system_version"] == 1 and pb["fault_class"] == "batch_abort"
    assert pb["embedding_model"] == "local-hash-v1" and len(pb["embedding"]) == 1024 and pb["stale"] is False

    # waggle narrated it; checkpoints exist for task threads
    assert wait_for(lambda: col("messages").find_one({"from": "waggle-1", "body": {"$regex": f"{inc['_id']} resolved"}}), 15, what="waggle resolution message")
    assert col("checkpoints").find_one({"thread_id": f"{inc['_id']}:scout"})
    assert col("checkpoints").find_one({"thread_id": s1["task_id"]})


def test_round2_recall_is_faster_and_triggers_upgrade(hive, fake):
    from common.db import col
    from swarm import recall

    first = col("incidents").find_one({"status": "resolved"})
    pb = col("playbooks").find_one({"incident": first["_id"]})
    # Atlas Search indexes are eventually consistent: wait until the playbook is searchable
    wait_for(lambda: any(h["_id"] == pb["_id"] for h in recall.search(pb["symptom_text"])[1]), 30, 0.5,
             "playbook to be indexed")

    fake.post("/_admin/reset")
    col("stats").update_one({"_id": "current"}, {"$set": {"run_id": "RUN-T2", "round": 2}}, upsert=True)
    fake.post("/mainframe/_chaos/batch_abort", json={"params": {}})
    inc = run_until_resolved(2, timeout=120)

    assert inc["_id"] != first["_id"] and inc["round"] == 2
    r = inc["recall"]
    assert r["used"] is True and r["hits"][0]["playbook_id"] == pb["_id"]
    assert r["hits"][0]["score"] >= 0.70 and r["hits"][0]["usable"] is True
    assert r["filter"] == {"embedding_model": "local-hash-v1", "stale": False}
    assert inc["diagnosis_path"] == ["mainframe"]          # one confirmatory hop instead of a 3-hop walk
    assert inc["metrics"]["hops"] < first["metrics"]["hops"]
    assert inc["metrics"]["diagnosis_s"] < first["metrics"]["diagnosis_s"]
    assert [s["op"] for s in inc["steps"]] == [s["op"] for s in first["steps"]]
    assert col("thoughts").find_one({"kind": "recall", "incident": inc["_id"], "text": {"$regex": "Recall hit"}})
    assert col("playbooks").find_one({"_id": pb["_id"]})["uses"] == 1

    # same fault class resolved twice -> Sentinel files ONE upgrade request for the Gardener
    upg = wait_for(lambda: col("upgrade_requests").find_one({"fault_class": "batch_abort"}), 15, 0.2, "upgrade request")
    assert upg["system"] == "mainframe" and upg["status"] == "requested"
    assert set(upg["evidence"]) == {first["_id"], inc["_id"]} and "malformed" in upg["weakness"]
    assert col("upgrade_requests").count_documents({}) == 1

    # Gardener-style hot reload bumps the version -> Sentinel marks mainframe playbooks stale
    fake.post("/_admin/reload/mainframe")
    wait_for(lambda: col("playbooks").count_documents({"system": "mainframe", "stale": False}) == 0, 15, 0.2,
             "playbooks to go stale")
    assert wait_for(lambda: col("thoughts").find_one({"kind": "learn", "text": {"$regex": "stale"}}), 15, what="stale-playbook narration")


def test_denied_approval_fails_step_and_escalates(hive, fake):
    from common.db import col, now

    fake.post("/_admin/reset")
    fake.post("/mainframe/_chaos/batch_abort", json={"params": {}})

    def pending():
        return col("approvals").find_one({"status": "pending"})
    apr = wait_for(pending, 90, 0.1, "a pending approval")
    col("approvals").update_one({"_id": apr["_id"], "status": "pending"},
                                {"$set": {"status": "denied", "decided_by": "test-beekeeper", "decided_at": now()}})
    inc = wait_for(lambda: col("incidents").find_one({"_id": apr["incident"], "status": "failed"}), 30, 0.1,
                   "incident to be escalated")
    assert inc["active"] is False and "denied" in inc["failure"]
    step = next(s for s in inc["steps"] if s["step_id"] == apr["step_id"])
    assert step["status"] == "failed" and step["attempts"] == []   # never sent
    assert col("ledger").count_documents({"step_id": apr["step_id"]}) == 0
    assert col("thoughts").find_one({"incident": inc["_id"], "text": {"$regex": "^Escalating"}})
    assert wait_for(lambda: col("messages").find_one({"body": {"$regex": f"{inc['_id']} escalated"}}), 5)
    # cooldown: the hive doesn't immediately re-open an incident for the systems the Beekeeper just declined
    import time
    deadline = time.monotonic() + 2
    while True:
        assert col("incidents").count_documents({"active": True}) == 0
        if time.monotonic() >= deadline:
            break
        time.sleep(.05)


def test_exploit_seen_in_logs_opens_security_incident_and_probe_verified_fix(hive, fake):
    """Health never shows an exploit: the Sentinel spots it in the logs, the probe confirms it, and the
    Guard verifies the fix by re-running the probe."""
    from common.db import col

    fake.post("/_admin/reset")  # clear the declined cascade from the previous test
    fake.post("/unix/_chaos/config_exploit", json={"params": {}})

    def resolved():
        from swarm.tests.conftest import approve_pending
        approve_pending()
        return col("incidents").find_one({"kind": "security", "status": "resolved"})
    inc = wait_for(resolved, 90, 0.2, "security incident resolved")
    assert inc["correlation_key"] == "security:unix" and inc["affected"] == ["unix"]
    assert inc["fault_class"] == "config_exploit" and inc["root_cause_system"] == "unix"
    assert inc["diagnosis_path"] == ["unix"]
    assert [s["op"] for s in inc["steps"]] == ["chmod_config"]
    assert "probe unix-world-writable-conf: exploitable=False" in inc["steps"][0]["evidence"]
    assert fake.post("/unix/security/probe/unix-world-writable-conf").json()["exploitable"] is False


def test_slow_apply_accepted_is_not_done(hive, fake):
    """An op that ACKs accepted/applied:false lands SLOW_APPLY_S later: the Guard keeps re-checking instead of
    rejecting, and verifies once the read-back shows it (no extra attempt)."""
    from common.db import col

    before = col("incidents").count_documents({"status": "resolved"})
    fake.post("/_admin/reset")
    fake.post("/unix/_chaos/slow_apply", json={"params": {"count": 1}})
    fake.post("/mainframe/_chaos/batch_abort", json={"params": {}})
    inc = run_until_resolved(before + 1, timeout=120)
    s3 = next(s for s in inc["steps"] if s["op"] == "rerun_transfer")
    assert s3["status"] == "verified" and len(s3["attempts"]) == 1
    assert s3["response"]["applied"] is False and s3["judged_at"] > s3["sent_at"]
    assert (s3["judged_at"] - s3["sent_at"]).total_seconds() >= 2.5   # waited for SLOW_APPLY_S (3s)
    assert col("thoughts").find_one({"incident": inc["_id"], "text": {"$regex": "^Accepted is not done"}})
    assert fake.get("/_fake/receipts").json()["receipts"][s3["action_id"]]["effective"] is True
