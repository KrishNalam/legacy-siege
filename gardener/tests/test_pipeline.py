"""End-to-end pipeline on an isolated DB and a temp copy of the fixture systems package.

Covers: plan (diff + evidence aggregation), test, approval gate (approve/deny via the DB),
deploy (atomic write, backup, ledger receipt in a transaction), prove (no incident ->
proven; incident -> stays deployed), revert, guardrails, and gardener.main registration.
The real systems/ directory and the live world/swarm DBs are never touched.
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest

from common.db import client, col, now
from gardener import main as gmain
from gardener import patcher, pipeline
from gardener.tests.conftest import SWARM_DB, WORLD_DB
from scripts import setup_db

FIX = Path(__file__).parent / "fixtures"
REAL_SYSTEMS = pipeline.SYSTEMS_DIR


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    from common import config
    assert config.SWARM_DB == SWARM_DB and config.WORLD_DB == WORLD_DB
    setup_db.ensure()  # real validators (ledger, agents) so our writes are checked against them
    yield
    for name in (SWARM_DB, WORLD_DB):
        client().drop_database(name)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the pipeline at a temp systems dir and a fake systems API."""
    systems = tmp_path / "systems"
    shutil.copytree(FIX / "systems", systems)
    monkeypatch.setattr(pipeline, "SYSTEMS_DIR", systems)
    monkeypatch.setattr(pipeline, "BACKUP_DIR", tmp_path / "backups")
    calls: list[str] = []
    version = {"v": 1}

    def fake_post(path, json=None, timeout=15):
        calls.append(path)
        if path.startswith("/_admin/reload/"):
            version["v"] += 1
            return {"version_before": version["v"] - 1, "version_after": version["v"]}
        return {"ok": True}

    monkeypatch.setattr(pipeline, "http_post", fake_post)
    for c in ("upgrade_requests", "approvals", "ledger", "incidents", "thoughts", "agents"):
        col(c).delete_many({})
    yield {"systems": systems, "calls": calls, "file": systems / "mainframe.py"}
    assert REAL_SYSTEMS.resolve() != systems.resolve()


def new_request(upg_id="UPG-0001", system="mainframe"):
    for i, rec in enumerate(["R-17", "R-42"], 1):
        col("incidents").insert_one({
            "_id": f"INC-000{i}", "active": False, "status": "resolved", "system": "portal",
            "affected": ["portal", "unix", "mainframe"], "root_cause_system": "mainframe",
            "fault_class": "batch_abort", "root_cause": "NIGHTBAT ABEND S0C7 on poison record",
            "opened_at": now(), "metrics": {"ttr_s": 40.0 + i},
            "steps": [{"step_id": f"INC-000{i}-s1", "status": "verified", "evidence": "ok",
                       "op": "quarantine_record", "system": "mainframe", "params": {"record_id": rec}}]})
    col("upgrade_requests").insert_one({
        "_id": upg_id, "system": system, "weakness": "NIGHTBAT accepts malformed records",
        "fault_class": "batch_abort", "evidence": ["INC-0001", "INC-0002"], "status": "requested", "ts": now()})
    return upg_id


def decide_later(upg_id, decision, delay=0.3):
    """Act as the Beekeeper: wait for the approval doc, then decide it (pending-only)."""
    def go():
        for _ in range(100):
            upg = col("upgrade_requests").find_one({"_id": upg_id}) or {}
            if upg.get("approval_id"):
                time.sleep(delay)
                col("approvals").update_one({"_id": upg["approval_id"], "status": "pending"},
                                            {"$set": {"status": decision, "decided_by": "beekeeper",
                                                      "decided_at": now()}})
                return
            time.sleep(0.05)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def test_plan_produces_small_diff_and_evidence(env):
    upg_id = new_request()
    doc = pipeline.plan(upg_id)
    assert doc["status"] == "planning" and doc["owner"] == "gardener-1"
    assert 0 < doc["diff_lines"] < 50 and doc["diff"].startswith("--- a/systems/mainframe.py")
    assert doc["evidence_summary"]["poison_records"] == ["R-17", "R-42"]
    assert doc["evidence_summary"]["total_ttr_s"] == 83.0
    assert "S0C7" in doc["explanation"]  # deterministic fallback (no LLM key)
    assert env["file"].read_text().count("return True") == 1  # planning never writes


def test_full_pipeline_approve_deploy_prove(env, monkeypatch):
    fired = []
    monkeypatch.setattr(pipeline, "fire_fault", lambda fault, target: fired.append((fault, target)) or "test")
    upg_id = new_request()
    original = env["file"].read_text()
    t = decide_later(upg_id, "approved")
    doc = pipeline.run(upg_id, approval_timeout=20, prove_window_s=1.0)
    t.join(5)
    assert doc["status"] == "proven", doc.get("reason")
    assert doc["tests"]["passed"] and doc["version_before"] == 1 and doc["version_after"] == 2
    assert doc["prove"]["incident_opened"] is False and fired == [("batch_abort", "mainframe")]
    # deployed file == original + exactly the approved diff
    new = env["file"].read_text()
    assert patcher.unified_diff(original, new, "systems/mainframe.py") == doc["diff"]
    assert Path(doc["backup"]).read_text() == original
    assert env["calls"] == ["/_admin/reload/mainframe"]
    apr = col("approvals").find_one({"_id": doc["approval_id"]})
    assert apr["kind"] == "upgrade" and apr["upgrade"] == upg_id and apr["risk"].startswith("low")
    led = col("ledger").find_one({"action_id": f"{upg_id}-deploy"})
    assert led["status"] == "verified" and led["approver"] == "beekeeper"
    assert led["params"]["diff_sha256"] == patcher.sha256(doc["diff"]) and led["version_after"] == 2
    kinds = {t["kind"] for t in col("thoughts").find({"agent": "gardener-1"})}
    assert kinds == {"upgrade"}
    # a second request for the same weakness is refused: already remediated
    col("upgrade_requests").insert_one({"_id": "UPG-0002", "system": "mainframe", "status": "requested",
                                        "evidence": [], "ts": now()})
    assert pipeline.run("UPG-0002")["status"] == "rejected"
    assert "already" in col("upgrade_requests").find_one({"_id": "UPG-0002"})["reason"]


def test_gate_denied_rejects_and_never_writes(env):
    upg_id = new_request()
    original = env["file"].read_text()
    pipeline.plan(upg_id)
    pipeline.test(upg_id)
    assert pipeline.gate(upg_id, wait=False) == "pending"
    assert col("upgrade_requests").find_one({"_id": upg_id})["status"] == "awaiting_approval"
    with pytest.raises(pipeline.StepError, match="not approved"):
        pipeline.deploy(upg_id)
    decide_later(upg_id, "denied", delay=0)
    with pytest.raises(pipeline.Rejected):
        pipeline.gate(upg_id, wait=True, timeout=10)
    doc = col("upgrade_requests").find_one({"_id": upg_id})
    assert doc["status"] == "rejected" and "denied by beekeeper" in doc["reason"]
    assert env["file"].read_text() == original and env["calls"] == []


def test_prove_with_incident_stays_deployed(env, monkeypatch):
    def fire_and_break(fault, target):
        col("incidents").insert_one({"_id": "INC-0099", "active": True, "status": "open", "system": "portal",
                                     "affected": ["portal"], "opened_at": now()})
        return "test"

    monkeypatch.setattr(pipeline, "fire_fault", fire_and_break)
    upg_id = new_request()
    decide_later(upg_id, "approved", delay=0)
    doc = pipeline.run(upg_id, approval_timeout=20, prove_window_s=3.0)
    assert doc["status"] == "deployed"
    assert doc["prove"]["incident_opened"] is True and doc["prove"]["incident"] == "INC-0099"


def test_revert_restores_original_and_reloads(env, monkeypatch):
    monkeypatch.setattr(pipeline, "fire_fault", lambda f, t: "test")
    original = env["file"].read_text()
    assert pipeline.revert("mainframe")["reverted"] is False  # no backup yet
    upg_id = new_request()
    decide_later(upg_id, "approved", delay=0)
    pipeline.run(upg_id, approval_timeout=20, prove_window_s=0.5)
    assert env["file"].read_text() != original
    res = pipeline.revert("mainframe")
    assert res["reverted"] and res["changed"] and env["file"].read_text() == original
    assert env["calls"][-1] == "/_admin/reload/mainframe" and res["reload"]["version_after"] == 3
    assert col("upgrade_requests").find_one({"_id": upg_id}).get("reverted_at")
    assert pipeline.revert("mainframe")["changed"] is False  # idempotent


def test_reload_failure_restores_original(env, monkeypatch):
    def boom(path, json=None, timeout=15):
        raise RuntimeError("systems down")

    upg_id = new_request()
    original = env["file"].read_text()
    pipeline.plan(upg_id)
    pipeline.test(upg_id)
    decide_later(upg_id, "approved", delay=0)
    assert pipeline.gate(upg_id, timeout=10) == "approved"
    monkeypatch.setattr(pipeline, "http_post", boom)
    with pytest.raises(pipeline.Rejected):
        pipeline.deploy(upg_id)
    assert env["file"].read_text() == original
    assert col("upgrade_requests").find_one({"_id": upg_id})["status"] == "rejected"


def test_guardrails(env, monkeypatch):
    with pytest.raises(patcher.PatchError, match="outside"):
        pipeline.inside_systems(env["systems"] / ".." / "evil.py")
    # no remediation for unix; missing function; oversized diff -> all rejected with a reason
    col("upgrade_requests").insert_many([
        {"_id": "UPG-0010", "system": "unix", "status": "requested", "evidence": [], "ts": now()},
        {"_id": "UPG-0011", "system": "mainframe", "status": "requested", "evidence": [], "ts": now()},
    ])
    assert pipeline.run("UPG-0010")["reason"].startswith("no predetermined remediation")
    env["file"].write_text("RECORD_LEN = 80\n\ndef run_batch(records):\n    return {}\n")
    assert "not found" in pipeline.run("UPG-0011")["reason"]
    shutil.copy(FIX / "systems" / "mainframe.py", env["file"])
    monkeypatch.setattr(patcher, "MAX_DIFF_LINES", 5)
    with pytest.raises(patcher.PatchError, match="refusing"):
        pipeline.build_patch("mainframe")


def test_source_changed_after_plan_blocks_deploy(env):
    upg_id = new_request()
    pipeline.plan(upg_id)
    env["file"].write_text(env["file"].read_text() + "\n# edited by someone else\n")
    with pytest.raises(pipeline.Rejected):
        pipeline.test(upg_id)
    assert "changed since the plan" in col("upgrade_requests").find_one({"_id": upg_id})["reason"]
    doc = pipeline.requeue(upg_id)  # e.g. the war room's "retry" after the source settled
    assert doc["status"] == "requested" and doc["reason"] is None
    assert pipeline.plan(upg_id)["status"] == "planning"


def test_missing_system_tests_reason_is_explicit(env):
    (env["systems"] / "tests" / "test_mainframe.py").unlink()
    doc = pipeline.run(new_request())
    assert doc["status"] == "rejected"
    assert "missing systems/tests/test_mainframe.py" in doc["reason"] and doc["tests"]["passed"] is False


def test_main_registers_heartbeats_and_picks_requests(env):
    gmain.register()
    a = col("agents").find_one({"_id": "gardener-1"})
    assert a["role"] == "gardener" and a["status"] == "idle" and a["target"] == "workshop" and a["generation"] == 1
    stop = threading.Event()
    monkey_hb = threading.Thread(target=gmain.heartbeat, args=(stop,), daemon=True)
    from common import config
    old = config.HEARTBEAT_S
    config.HEARTBEAT_S = 0.1
    try:
        before = a["last_seen"]
        monkey_hb.start()
        time.sleep(0.4)
        assert col("agents").find_one({"_id": "gardener-1"})["last_seen"] > before
    finally:
        stop.set()
        config.HEARTBEAT_S = old
    assert gmain.next_request() is None
    new_request("UPG-0005")
    assert gmain.next_request()["_id"] == "UPG-0005"
    stream = gmain.open_stream()
    col("upgrade_requests").insert_one({"_id": "UPG-0006", "system": "mainframe", "status": "requested",
                                        "evidence": [], "ts": now()})
    stream, woke = gmain.wait_for_request(stream, 5)
    stream.close()
    assert woke


def test_reset_while_awaiting_approval_unblocks_the_gardener(env):
    """A full reset wipes the approval the Gardener is waiting on: it must give up and go back to watching
    for new requests (not wait forever), so the next upgrade request after the reset is picked up."""
    upg_id = new_request()
    done = threading.Event()
    t = threading.Thread(target=lambda: (gmain.run_safely(upg_id), done.set()), daemon=True)
    t.start()
    for _ in range(400):
        if (col("upgrade_requests").find_one({"_id": upg_id}) or {}).get("status") == "awaiting_approval":
            break
        time.sleep(0.05)
    assert col("upgrade_requests").find_one({"_id": upg_id})["status"] == "awaiting_approval"
    time.sleep(0.5)
    assert not done.is_set()  # really waiting for the Beekeeper
    setup_db.reset()  # the demo's Reset ▸ Full
    assert done.wait(10), "gardener still blocked on a wiped approval"
    assert col("upgrade_requests").count_documents({}) == 0  # nothing resurrected or marked rejected
    new_request("UPG-0001")  # counters restart after a reset: same id, new request
    assert gmain.next_request()["_id"] == "UPG-0001"


def test_wait_for_decision_abandons_when_request_moves_on(env):
    upg_id = new_request()
    pipeline.plan(upg_id)
    pipeline.test(upg_id)
    pipeline.gate(upg_id, wait=False)
    apr_id = col("upgrade_requests").find_one({"_id": upg_id})["approval_id"]
    col("upgrade_requests").delete_one({"_id": upg_id})  # request gone, approval still pending
    t0 = time.monotonic()
    with pytest.raises(pipeline.Abandoned):
        pipeline.wait_for_decision(apr_id, timeout=10, upg_id=upg_id)
    assert time.monotonic() - t0 < 5
