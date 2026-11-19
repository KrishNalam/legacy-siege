"""Failure memory (agents/lessons.py) against local MongoDB: write, read back into a plan, skip, stale."""
from __future__ import annotations

import pytest


@pytest.fixture()
def mem(db, monkeypatch):
    monkeypatch.setenv("FAILURE_MEMORY", "1")
    for n in ("lessons", "playbooks"):
        db[n].delete_many({})
    from agents import lessons
    yield lessons
    for n in ("lessons", "playbooks"):
        db[n].delete_many({})


def _step(op="quarantine_record", system="mainframe", n=1, **kw):
    return {"step_id": f"INC-0001-s{n}", "n": n, "system": system, "op": op, "params": {"record_id": "REC-00042"},
            "attempts": ["INC-0001-s1-a1"], "response": {"accepted": True, "applied": True}, **kw}


def _plan():
    from agents import policy
    raw = policy.fix_plan("batch_abort", {"poison_records": ["REC-00077"]})
    return [{"step_id": f"INC-0002-s{i}", "n": i, **s} for i, s in enumerate(raw, 1)]


def test_switch_defaults_off(monkeypatch):
    from agents import lessons
    monkeypatch.delenv("FAILURE_MEMORY", raising=False)
    assert not lessons.enabled()
    monkeypatch.setenv("FAILURE_MEMORY", "1")
    assert lessons.enabled()


def test_classify():
    from agents.lessons import classify
    assert classify("ACK ≠ APPLIED: state.batch.quarantined=[]", {"applied": True}) == "lied"
    assert classify("mainframe refused restart_batch: poison remains", {"accepted": False}) == "refused"
    assert classify("restart_batch accepted but never applied after 31s: x", {"accepted": True}) == "not_applied"
    assert classify("no ACK from unix (timeout); state shows y", {"error": "timeout"}) == "no_ack"


def test_record_failure_is_durable_versioned_and_counts_repeats(mem, db):
    ls = mem.record_failure("INC-0001", _step(), "INC-0001-s1-a1", "ACK ≠ APPLIED: state.batch.quarantined=[]",
                            "verifier-1", version=1, fault_class="batch_abort")
    assert ls["_id"] == "LS:mainframe:v1:quarantine_record:lied"
    assert ls["kind"] == "lied" and ls["system_version"] == 1 and ls["stale"] is False and ls["count"] == 1
    assert ls["incidents"] == ["INC-0001"] and ls["history"][0]["action_id"] == "INC-0001-s1-a1"
    assert "applied:true" in ls["why"] and ls["fault_class"] == "batch_abort"
    again = mem.record_failure("INC-0009", _step(), "INC-0009-s1-a1", "ACK ≠ APPLIED: still poisoned", "verifier-1",
                               version=1)
    assert again["count"] == 2 and again["incidents"] == ["INC-0001", "INC-0009"] and len(again["history"]) == 2
    assert db.lessons.count_documents({}) == 1
    # a different version is a different lesson
    mem.record_failure("INC-0010", _step(), "INC-0010-s1-a1", "ACK ≠ APPLIED: x", "verifier-1", version=2)
    assert db.lessons.count_documents({}) == 2


def test_validator_rejects_a_lesson_without_why(mem, db):
    from pymongo.errors import WriteError
    with pytest.raises(WriteError):
        db.lessons.insert_one({"_id": "LS:x", "kind": "lied", "system": "unix", "system_version": 1, "why": "",
                               "stale": False, "first_seen": None})


def test_plan_reads_matching_lessons_and_marks_steps(mem, db):
    mem.record_failure("INC-0001", _step(), "INC-0001-s1-a1", "ACK ≠ APPLIED: state.batch.quarantined=[]",
                       "verifier-1", version=1)
    plan = _plan()
    applied, lines = mem.for_plan(plan, lambda s: 1, "INC-0002")
    assert [ls["_id"] for ls in applied] == ["LS:mainframe:v1:quarantine_record:lied"]
    q = next(s for s in plan if s["op"] == "quarantine_record")
    assert q["distrust_ack"] is True and q["lessons"][0]["id"] == applied[0]["_id"]
    assert all("lessons" not in s for s in plan if s["op"] != "quarantine_record")
    assert len(lines) == 1 and "Not trusting its ACK" in lines[0] and "INC-0001" in lines[0]
    doc = db.lessons.find_one({"_id": applied[0]["_id"]})
    assert doc["uses"] == 1 and doc["used_in"] == ["INC-0002"] and doc["last_used_at"] is not None


def test_version_bump_marks_lessons_stale_and_plan_says_so(mem, db):
    mem.record_failure("INC-0001", _step(), "INC-0001-s1-a1", "ACK ≠ APPLIED: x", "verifier-1", version=1)
    assert mem.mark_stale("mainframe", 2) == 1
    assert mem.mark_stale("mainframe", 2) == 0  # idempotent
    doc = db.lessons.find_one()
    assert doc["stale"] is True and doc["stale_reason"] == "mainframe upgraded to v2"
    plan = _plan()
    applied, lines = mem.for_plan(plan, lambda s: 2, "INC-0003")
    assert applied == [] and all("distrust_ack" not in s for s in plan)
    assert "stale, not applying it" in lines[0] and "v1" in lines[0] and "v2" in lines[0]


def test_version_mismatch_is_never_applied_even_if_not_yet_marked(mem):
    mem.record_failure("INC-0001", _step(), "INC-0001-s1-a1", "ACK ≠ APPLIED: x", "verifier-1", version=1)
    plan = _plan()
    applied, lines = mem.for_plan(plan, lambda s: 3, "INC-0004")
    assert applied == [] and "stale" in lines[0]


def test_recovery_is_noted(mem, db):
    mem.record_failure("INC-0001", _step(), "INC-0001-s1-a1", "ACK ≠ APPLIED: x", "verifier-1", version=1)
    st = _step(attempts=["INC-0001-s1-a1", "INC-0001-s1-a2"])
    assert mem.note_recovery("INC-0001", st, "INC-0001-s1-a2") == 1
    assert "INC-0001-s1-a2" in db.lessons.find_one()["recovered_by"]
    assert mem.note_recovery("INC-0001", _step(attempts=["only-one"]), "x") == 0


def test_misfit_playbook_is_skipped_for_the_same_symptoms(mem, db):
    sym = "portal degraded, blames unix; unix degraded, blames mainframe"
    hit_bad = {"playbook_id": "PB-0001", "system": "unix", "fault_class": "disk_fill", "fix": []}
    ls = mem.record_misfit("INC-0005", hit_bad, sym, {"fault_class": None, "facts": ["disk_pct=40"]},
                           "investigator-1", version=1)
    assert ls["_id"] == "LS:unix:v1:PB-0001:misfit" and ls["kind"] == "misfit" and ls["playbook"] == "PB-0001"
    hits = [{"_id": "PB-0001", "score": 0.97}, {"_id": "PB-0002", "score": 0.91}]
    summary = [{"playbook_id": "PB-0001", "score": 0.97, "usable": True},
               {"playbook_id": "PB-0002", "score": 0.91, "usable": True}]
    usable, lines = mem.skip_misfits(hits, summary, hits[0], sym, "INC-0006")
    assert usable["_id"] == "PB-0002"
    assert summary[0]["usable"] is False and summary[0]["skipped_by"] == ls["_id"]
    assert "Skipping PB-0001" in lines[0] and "INC-0005" in lines[0]
    assert db.lessons.find_one({"_id": ls["_id"]})["uses"] == 1
    # other symptoms: no skip
    summary2 = [{"playbook_id": "PB-0001", "score": 0.97, "usable": True}]
    usable2, lines2 = mem.skip_misfits(hits[:1], summary2, hits[0], "something else entirely", "INC-0007")
    assert usable2["_id"] == "PB-0001" and lines2 == []


def test_memory_items_list_every_kind_with_versions(mem, db):
    from common.db import now
    db.playbooks.insert_one({"_id": "PB-0001", "system": "mainframe", "system_version": 1, "fault_class": "batch_abort",
                             "root_cause": "poison record", "fix": [{"op": "quarantine_record"}], "stale": True,
                             "stale_reason": "mainframe upgraded to v2", "uses": 1, "created_at": now(),
                             "symptom_text": "sym"})
    mem.record_failure("INC-0001", _step(), "INC-0001-s1-a1", "ACK ≠ APPLIED: x", "verifier-1", version=1)
    rows = mem.items({"mainframe": 2})
    kinds = {r["kind"]: r for r in rows}
    assert set(kinds) >= {"playbook", "lesson"}
    pb, ls = kinds["playbook"], kinds["lesson"]
    assert pb["system_version"] == 1 and pb["current_version"] == 2 and pb["stale"] is True and pb["uses"] == 1
    assert ls["lesson_kind"] == "lied" and ls["system_version"] == 1 and ls["current_version"] == 2
    assert "uses" in ls and "last_used_at" in ls
    assert "stale" in mem.stale_note("sym") and "PB-0001" in mem.stale_note("sym")
    assert mem.stale_note("nothing like it") is None
