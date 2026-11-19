"""W19 UPGRADER_HELP: Monitor #2 repeat hand-off starts the vetted pipeline, Operators' help answers, Investigator push."""
from types import SimpleNamespace
from unittest.mock import Mock

from common.db import now
from upgrader import help as gh
from agents.roles import monitor


def _clear(db):
    for c in ("gardener_requests", "gardener_notes", "upgrade_requests", "playbooks", "lessons", "incidents",
              "thoughts", "ledger"):
        db[c].delete_many({})


def test_switch_off_files_nothing(db, monkeypatch):
    _clear(db)
    monkeypatch.delenv("UPGRADER_HELP", raising=False)
    monkeypatch.delenv("AGENTS_COMMS", raising=False)
    monkeypatch.setattr(monitor, "col", Mock(side_effect=AssertionError("off must not read DB")))
    monitor.note_repeated_problems(None)
    assert not gh.enabled() and db.gardener_requests.count_documents({}) == 0


def test_repeat_hands_off_once_and_starts_or_requeues_upgrade(db, monkeypatch):
    _clear(db)
    monkeypatch.setenv("UPGRADER_HELP", "1")
    monkeypatch.delenv("AGENTS_COMMS", raising=False)
    db.incidents.insert_many([{"_id": f"INC-{i}", "system": "portal", "root_cause_system": "mainframe",
                               "fault_class": "batch_abort", "symptoms": ["mainframe down"], "status": "resolved",
                               "opened_at": now()} for i in range(3)])
    monkeypatch.setattr(monitor, "say", Mock())
    agent = SimpleNamespace(id="monitor-1", think=Mock())
    monitor.note_repeated_problems(agent)
    monitor.note_repeated_problems(agent)
    reqs = list(db.gardener_requests.find())
    assert len(reqs) == 1 and reqs[0]["kind"] == "repeat" and reqs[0]["count"] == 3
    assert any("Monitor #2 → Upgrader: batch_abort on mainframe repeated 3×" in c.args[0]
               for c in agent.think.call_args_list)
    done = gh.handle(reqs[0])  # no upgrade yet -> the Upgrader files one for its existing pipeline
    upg = db.upgrade_requests.find_one({"system": "mainframe", "fault_class": "batch_abort"})
    assert done["status"] == "answered" and upg["status"] == "requested" and done["answer"]["upgrade"] == upg["_id"]
    assert db.gardener_notes.find_one()["upgrader_ack"]["request"] == reqs[0]["_id"]
    # a rejected upgrade (e.g. denied once) is re-queued by the next repeat request
    db.upgrade_requests.update_one({"_id": upg["_id"]}, {"$set": {"status": "rejected", "reason": "denied"}})
    db.gardener_requests.update_one({"_id": reqs[0]["_id"]}, {"$set": {"status": "open"}})
    ans = gh.handle(db.gardener_requests.find_one())["answer"]
    assert "re-queued" in ans["text"] and db.upgrade_requests.find_one()["status"] == "requested"
    # nothing vetted for a lying ACK: noted only
    assert gh.answer_repeat({"_id": "x", "system": "unix", "fault_class": None, "problem": "lies"})["kind"] == "none"


def test_repeat_counts_resolved_incidents_once_per_night(db, monkeypatch):
    """RV1b: Red re-strikes within one night and failed incidents must not reach 3 and start an upgrade early."""
    _clear(db)
    monkeypatch.setenv("UPGRADER_HELP", "1")
    monkeypatch.delenv("AGENTS_COMMS", raising=False)
    base = {"system": "portal", "root_cause_system": "mainframe", "fault_class": "batch_abort",
            "symptoms": ["mainframe down"], "opened_at": now()}
    db.incidents.insert_many([{**base, "_id": f"INC-{i}", "status": "resolved", "run_id": "RUN-0001"} for i in range(3)]
                             + [{**base, "_id": "INC-F", "status": "failed", "run_id": "RUN-0002"},
                                {**base, "_id": "INC-4", "status": "resolved", "run_id": "RUN-0002"}])
    monkeypatch.setattr(monitor, "say", Mock())
    agent = SimpleNamespace(id="monitor-1", think=Mock())
    monitor.note_repeated_problems(agent)
    assert db.gardener_requests.count_documents({}) == 0 and db.upgrade_requests.count_documents({}) == 0
    db.incidents.insert_one({**base, "_id": "INC-5", "status": "resolved", "run_id": "RUN-0003"})
    monitor.note_repeated_problems(agent)
    reqs = list(db.gardener_requests.find())
    assert len(reqs) == 1 and reqs[0]["count"] == 3


def test_help_answers_playbook_params_then_lying_runbook(db, monkeypatch):
    _clear(db)
    db.playbooks.insert_one({"_id": "PB-0001", "system": "mainframe", "system_version": 1, "stale": False,
                             "incident": "INC-0001", "verified_at": now(),
                             "fix": [{"system": "mainframe", "op": "restart_job", "params": {"job": "NIGHTBAT"}}]})
    step = {"step_id": "INC-0002-s1", "system": "mainframe", "op": "restart_job", "params": {"job": "X"},
            "attempts": ["INC-0002-s1-a1", "INC-0002-s1-a2"]}
    ev = ["INC-0002-s1-a1 failed: ACK ≠ APPLIED: batch ABENDED", "INC-0002-s1-a2 failed: ACK ≠ APPLIED: batch ABENDED"]
    req, new = gh.ask("operator-1", "INC-0002", step, ev, "batch_abort")
    assert new and gh.ask("operator-1", "INC-0002", step, ev, "batch_abort")[1] is False  # idempotent
    ans = gh.handle(req)["answer"]
    assert ans["kind"] == "playbook" and ans["advice"]["params"] == {"job": "NIGHTBAT"}
    assert gh.await_answer(req["_id"], 0) == ans
    step2 = {**step, "step_id": "INC-0002-s2", "op": "quarantine_record", "params": {"record_id": "R1"}}
    ans2 = gh.handle(gh.ask("operator-1", "INC-0002", step2, ev, "batch_abort")[0])["answer"]
    assert ans2["kind"] == "runbook" and ans2["advice"] == {"fast": True}
    step3 = {**step2, "step_id": "INC-0002-s3", "system": "unix", "op": "flush_queue"}
    assert gh.handle(gh.ask("w", "INC-0002", step3, ["a1 failed: refused"], None)[0])["answer"]["kind"] == "none"


def test_sentinel2_pushes_known_fix_to_investigator(db, monkeypatch):
    _clear(db)
    root = "mainframe down, blames nothing upstream, batch abended"
    db.playbooks.insert_one({"_id": "PB-0001", "system": "mainframe", "system_version": 1, "stale": False,
                             "incident": "INC-0001", "fault_class": "batch_abort", "verified_at": now(),
                             "symptoms": ["portal degraded, blames unix", root],
                             "fix": [{"system": "mainframe", "op": "quarantine_record", "params": {}}]})
    db.incidents.insert_many([
        {"_id": "INC-0002", "active": True, "kind": "chain", "owner": "investigator-1", "status": "claimed",
         "symptoms": ["portal degraded, blames unix", root], "system": "mainframe", "opened_at": now(), "correlation_key": "chain:mainframe"},
        {"_id": "INC-0003", "active": True, "kind": "chain", "owner": "investigator-2", "status": "diagnosing",
         "symptoms": ["portal degraded, blames unix"], "system": "portal", "opened_at": now(),
         "correlation_key": "chain:portal"}])  # a shared downstream line alone is not a match
    monkeypatch.setattr(monitor, "say", Mock())
    agent = SimpleNamespace(id="monitor-1", think=Mock())
    monitor.push_known_fixes(agent, {"mainframe": 1})
    monitor.push_known_fixes(agent, {"mainframe": 1})
    assert monitor.known_fix_for([root], {"mainframe": 1})["_id"] == "PB-0001"  # early part naming the root
    assert db.incidents.find_one({"_id": "INC-0002"})["known_fix"]["playbook_id"] == "PB-0001"
    assert "known_fix" not in db.incidents.find_one({"_id": "INC-0003"})
    assert agent.think.call_count == 1 and "Monitor #2 → investigator-1" in agent.think.call_args.args[0]
    assert monitor.known_fix_for([root], {"mainframe": 2}) is None  # learned on v1, mainframe is v2 now


def test_monitor_2_is_its_own_agent_only_with_switch(db, monkeypatch):
    """Structure v3: UPGRADER_HELP=1 adds monitor-2 to the roster; monitor-1 hands over the Monitor #2 duties
    only while monitor-2 is alive (and takes them back when it dies)."""
    from datetime import timedelta
    from agents import supervisor
    monkeypatch.delenv("AGENTS_ROSTER", raising=False)
    monkeypatch.delenv("UPGRADER_HELP", raising=False)
    assert ("monitor", "monitor-2") not in supervisor.roster()
    monkeypatch.setenv("UPGRADER_HELP", "1")
    assert ("monitor", "monitor-2") in supervisor.roster() and ("monitor", "monitor-1") in supervisor.roster()
    db.agents.delete_many({"_id": "monitor-2"})
    assert not monitor.monitor_2_alive()
    db.agents.insert_one({"_id": "monitor-2", "role": "monitor", "status": "working", "last_seen": now()})
    assert monitor.monitor_2_alive()
    db.agents.update_one({"_id": "monitor-2"}, {"$set": {"last_seen": now() - timedelta(minutes=5)}})
    assert not monitor.monitor_2_alive()
    db.agents.delete_many({"_id": "monitor-2"})
