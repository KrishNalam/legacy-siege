"""UPGRADER_HELP: the Upgrader accepts toolsmith tools (W20 envelope) and ships one as an upgrade.

Switch off: an upgrade with no code remediation is not turned into a tool upgrade. Switch on: a
Monitor #2 repeat that no code fix covers starts a tool upgrade, which runs the real sandbox replay,
waits for the approver, adopts the tool and is proven only by a swarm fix whose tool steps were all
Verifier-verified. Tampered or retired tools are never used. Isolated DBs only.
"""
from __future__ import annotations

import pytest

from common.db import client, col, now
from upgrader import help as help_desk
from upgrader import pipeline, tools
from upgrader.tests.conftest import SWARM_DB, WORLD_DB
from upgrader.tests.test_pipeline import decide_later
from scripts import setup_db

TOOL_ID = "TOOL:disk_fill:v1:1"


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    setup_db.ensure()
    yield
    for name in (SWARM_DB, WORLD_DB):
        client().drop_database(name)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("UPGRADER_HELP", raising=False)
    for c in ("upgrade_requests", "approvals", "ledger", "incidents", "thoughts", "agents", "tools",
              "gardener_requests", "gardener_notes"):
        col(c).delete_many({})
    col("systems").delete_many({"_id": "unix"})
    col("systems").insert_one({"_id": "unix", "version": 1, "status": "up"})


def add_tool(ops=None, status="active", tid=TOOL_ID):
    col("tools").insert_one({
        "_id": tid, "name": "disk_fill-recovery", "fault_class": "disk_fill", "version": 1, "status": status,
        "ops": tools.whitelist("disk_fill") if ops is None else ops, "source": "policy", "successes": 2,
        "preconditions": {"system": "unix", "system_version": 1, "fault_class": "disk_fill",
                          "requires_current_diagnosis": True},
        "built_from": [{"lesson": "L1", "count": 3}]})


def tool_request(tid=TOOL_ID, status="active"):
    tool = col("tools").find_one({"_id": tid})
    rid = f"GT-{tid}:{status}"
    col("gardener_requests").insert_one({
        "_id": rid, "kind": "tool", "from": "toolsmith-1", "system": "unix", "problem": "disk_fill",
        "fault_class": "disk_fill", "payload": {"tool_id": tid, "name": tool["name"], "status": status,
                                                "ops": tool["ops"], "preconditions": tool["preconditions"]},
        "ts": now(), "status": "open", "answer": None})
    return help_desk.handle({"_id": rid})


def test_tool_requests_accept_refuse_drop():
    add_tool()
    doc = tool_request()
    assert doc["status"] == "answered" and doc["answer"]["kind"] == "tool"
    assert col("tools").find_one({"_id": TOOL_ID})["upgrader"]["accepted"] is True
    assert tools.accepted("unix", "disk_fill")["_id"] == TOOL_ID

    bad = tools.whitelist("disk_fill")
    bad[0] = {**bad[0], "op": "chmod_config"}  # anything off the vetted whitelist is refused
    add_tool(ops=bad, tid="TOOL:disk_fill:v1:2")
    assert tool_request("TOOL:disk_fill:v1:2")["answer"]["kind"] == "none"
    assert tools.accepted("unix", "disk_fill")["_id"] == TOOL_ID

    col("tools").update_one({"_id": TOOL_ID}, {"$set": {"status": "retired"}})
    assert tool_request(status="retired")["answer"]["text"].startswith("dropped")
    assert tools.accepted("unix", "disk_fill") is None


def test_switch_off_never_turns_an_upgrade_into_a_tool_upgrade():
    add_tool()
    tool_request()
    col("upgrade_requests").insert_one({"_id": "UPG-0009", "system": "unix", "fault_class": "disk_fill",
                                        "status": "planning", "ts": now()})
    assert "remediation_kind" not in pipeline._pick_kind(col("upgrade_requests").find_one({"_id": "UPG-0009"}))


def test_repeat_ships_tool_through_gated_proven_upgrade(monkeypatch):
    monkeypatch.setenv("UPGRADER_HELP", "1")
    add_tool()
    tool_request()
    col("gardener_requests").insert_one({
        "_id": "GR-unix-disk_fill", "kind": "repeat", "from": "monitor-1", "system": "unix", "problem": "disk_fill",
        "fault_class": "disk_fill", "count": 3, "evidence": [], "note": "unix-disk_fill", "status": "open",
        "ts": now(), "answer": None})
    ans = help_desk.handle({"_id": "GR-unix-disk_fill"})["answer"]
    assert ans["kind"] == "upgrade"
    upg_id = ans["upgrade"]
    assert col("upgrade_requests").find_one({"_id": upg_id})["remediation_kind"] == "tool"

    def swarm_fixes_it(fault, target):  # the swarm resolves the replayed fault with the tool's steps
        col("incidents").insert_one({
            "_id": "INC-0100", "status": "resolved", "fault_class": fault, "system": target, "opened_at": now(),
            "steps": [{"step_id": f"INC-0100-s{i}", "status": "verified", "evidence": "read back", "tool_id": TOOL_ID, **op}
                      for i, op in enumerate(tools.whitelist(fault), 1)]})
        return "test"

    monkeypatch.setattr(pipeline, "fire_fault", swarm_fixes_it)
    decide_later(upg_id, "approved")
    doc = pipeline.run(upg_id, approval_timeout=20)
    assert doc["status"] == "proven", (doc.get("reason"), doc.get("prove"))
    assert doc["tests"]["replay_passed"] is True  # real sandbox: disk_fill fired, tool ran, unix read back up
    assert doc["prove"]["incident"] == "INC-0100"
    assert col("tools").find_one({"_id": TOOL_ID})["upgrader"]["adopted"] == upg_id
    assert col("ledger").find_one({"action_id": f"{upg_id}-deploy"})["op"] == "adopt_tool"

    # an Operator whose purge_spool failed twice is pointed at the accepted tool
    col("gardener_requests").insert_one({
        "_id": "GH-INC-0101-s1", "kind": "help", "from": "operator-1", "incident": "INC-0101", "step_id": "s1",
        "system": "unix", "op": "purge_spool", "params": {"path": "/var/spool/xfer"}, "attempts": [],
        "evidence": [], "fault_class": "disk_fill", "status": "open", "ts": now(), "answer": None})
    assert help_desk.handle({"_id": "GH-INC-0101-s1"})["answer"]["kind"] == "tool"


def test_tool_upgrade_not_proven_when_swarm_skips_the_tool(monkeypatch):
    monkeypatch.setenv("UPGRADER_HELP", "1")
    add_tool()
    tool_request()
    col("upgrade_requests").insert_one({"_id": "UPG-0010", "system": "unix", "fault_class": "disk_fill",
                                        "status": "deployed", "remediation_kind": "tool", "tool_id": TOOL_ID,
                                        "ts": now()})
    monkeypatch.setattr(pipeline, "fire_fault", lambda f, t: col("incidents").insert_one({
        "_id": "INC-0200", "status": "resolved", "fault_class": f, "system": t, "opened_at": now(),
        "steps": [{"step_id": "INC-0200-s1", "status": "verified", "evidence": "read back", "op": "purge_spool",
                   "system": "unix"}]}) and "test")
    doc = pipeline.prove("UPG-0010")
    assert doc["status"] == "deployed" and "without the tool" in doc["prove"]["verdict"]
