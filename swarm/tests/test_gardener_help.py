"""W19 GARDENER_HELP: Sentinel #2 repeat hand-off starts the vetted pipeline, Workers' help answers, Scout push."""
from types import SimpleNamespace
from unittest.mock import Mock

from common.db import now
from gardener import help as gh
from swarm.roles import sentinel


def _clear(db):
    for c in ("gardener_requests", "gardener_notes", "upgrade_requests", "playbooks", "lessons", "incidents",
              "thoughts", "ledger"):
        db[c].delete_many({})


def test_switch_off_files_nothing(db, monkeypatch):
    _clear(db)
    monkeypatch.delenv("GARDENER_HELP", raising=False)
    monkeypatch.delenv("SWARM_COMMS", raising=False)
    monkeypatch.setattr(sentinel, "col", Mock(side_effect=AssertionError("off must not read DB")))
    sentinel.note_repeated_problems(None)
    assert not gh.enabled() and db.gardener_requests.count_documents({}) == 0


def test_repeat_hands_off_once_and_starts_or_requeues_upgrade(db, monkeypatch):
    _clear(db)
    monkeypatch.setenv("GARDENER_HELP", "1")
    monkeypatch.delenv("SWARM_COMMS", raising=False)
    db.incidents.insert_many([{"_id": f"INC-{i}", "system": "portal", "root_cause_system": "mainframe",
                               "fault_class": "batch_abort", "symptoms": ["mainframe down"], "status": "resolved",
                               "opened_at": now()} for i in range(3)])
    monkeypatch.setattr(sentinel, "say", Mock())
    agent = SimpleNamespace(id="sentinel-1", think=Mock())
    sentinel.note_repeated_problems(agent)
    sentinel.note_repeated_problems(agent)
    reqs = list(db.gardener_requests.find())
    assert len(reqs) == 1 and reqs[0]["kind"] == "repeat" and reqs[0]["count"] == 3
    assert any("Sentinel #2 → Gardener: batch_abort on mainframe repeated 3×" in c.args[0]
               for c in agent.think.call_args_list)
    done = gh.handle(reqs[0])  # no upgrade yet -> the Gardener files one for its existing pipeline
    upg = db.upgrade_requests.find_one({"system": "mainframe", "fault_class": "batch_abort"})
    assert done["status"] == "answered" and upg["status"] == "requested" and done["answer"]["upgrade"] == upg["_id"]
    assert db.gardener_notes.find_one()["gardener_ack"]["request"] == reqs[0]["_id"]
    # a rejected upgrade (e.g. denied once) is re-queued by the next repeat request
    db.upgrade_requests.update_one({"_id": upg["_id"]}, {"$set": {"status": "rejected", "reason": "denied"}})
    db.gardener_requests.update_one({"_id": reqs[0]["_id"]}, {"$set": {"status": "open"}})
    ans = gh.handle(db.gardener_requests.find_one())["answer"]
    assert "re-queued" in ans["text"] and db.upgrade_requests.find_one()["status"] == "requested"
    # nothing vetted for a lying ACK: noted only
    assert gh.answer_repeat({"_id": "x", "system": "unix", "fault_class": None, "problem": "lies"})["kind"] == "none"


def test_help_answers_playbook_params_then_lying_runbook(db, monkeypatch):
    _clear(db)
    db.playbooks.insert_one({"_id": "PB-0001", "system": "mainframe", "system_version": 1, "stale": False,
                             "incident": "INC-0001", "verified_at": now(),
                             "fix": [{"system": "mainframe", "op": "restart_job", "params": {"job": "NIGHTBAT"}}]})
    step = {"step_id": "INC-0002-s1", "system": "mainframe", "op": "restart_job", "params": {"job": "X"},
            "attempts": ["INC-0002-s1-a1", "INC-0002-s1-a2"]}
    ev = ["INC-0002-s1-a1 failed: ACK ≠ APPLIED: batch ABENDED", "INC-0002-s1-a2 failed: ACK ≠ APPLIED: batch ABENDED"]
    req, new = gh.ask("worker-1", "INC-0002", step, ev, "batch_abort")
    assert new and gh.ask("worker-1", "INC-0002", step, ev, "batch_abort")[1] is False  # idempotent
    ans = gh.handle(req)["answer"]
    assert ans["kind"] == "playbook" and ans["advice"]["params"] == {"job": "NIGHTBAT"}
    assert gh.await_answer(req["_id"], 0) == ans
    step2 = {**step, "step_id": "INC-0002-s2", "op": "quarantine_record", "params": {"record_id": "R1"}}
    ans2 = gh.handle(gh.ask("worker-1", "INC-0002", step2, ev, "batch_abort")[0])["answer"]
    assert ans2["kind"] == "runbook" and ans2["advice"] == {"fast": True}
    step3 = {**step2, "step_id": "INC-0002-s3", "system": "unix", "op": "flush_queue"}
    assert gh.handle(gh.ask("w", "INC-0002", step3, ["a1 failed: refused"], None)[0])["answer"]["kind"] == "none"


def test_sentinel2_pushes_known_fix_to_scout(db, monkeypatch):
    _clear(db)
    root = "mainframe down, blames nothing upstream, batch abended"
    db.playbooks.insert_one({"_id": "PB-0001", "system": "mainframe", "system_version": 1, "stale": False,
                             "incident": "INC-0001", "fault_class": "batch_abort", "verified_at": now(),
                             "symptoms": ["portal degraded, blames unix", root],
                             "fix": [{"system": "mainframe", "op": "quarantine_record", "params": {}}]})
    db.incidents.insert_many([
        {"_id": "INC-0002", "active": True, "kind": "chain", "owner": "scout-1", "status": "claimed",
         "symptoms": ["portal degraded, blames unix", root], "system": "mainframe", "opened_at": now(), "correlation_key": "chain:mainframe"},
        {"_id": "INC-0003", "active": True, "kind": "chain", "owner": "scout-2", "status": "diagnosing",
         "symptoms": ["portal degraded, blames unix"], "system": "portal", "opened_at": now(),
         "correlation_key": "chain:portal"}])  # a shared downstream line alone is not a match
    monkeypatch.setattr(sentinel, "say", Mock())
    agent = SimpleNamespace(id="sentinel-1", think=Mock())
    sentinel.push_known_fixes(agent, {"mainframe": 1})
    sentinel.push_known_fixes(agent, {"mainframe": 1})
    assert db.incidents.find_one({"_id": "INC-0002"})["known_fix"]["playbook_id"] == "PB-0001"
    assert "known_fix" not in db.incidents.find_one({"_id": "INC-0003"})
    assert agent.think.call_count == 1 and "Sentinel #2 → scout-1" in agent.think.call_args.args[0]
    assert sentinel.known_fix_for([root], {"mainframe": 1})["_id"] == "PB-0001"  # early part naming the root
    assert sentinel.known_fix_for([root], {"mainframe": 2}) is None  # learned on v1, mainframe is v2 now
