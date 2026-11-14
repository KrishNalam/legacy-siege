"""AI second opinion: the judge's verdicts, and the background thread's no-key / offline / done paths."""
from __future__ import annotations

import time

import pytest

from swarm.tests.conftest import reset_runtime  # noqa: F401  (sets test env first)

from common.db import col, now  # noqa: E402
from swarm import policy, second_opinion as so  # noqa: E402

PLAN = [dict(s, n=i) for i, s in enumerate(policy.fix_plan("batch_abort", {"poison_records": ["TX000042"]}), 1)]
READBACK = {"mainframe": {"batch": {"status": "ABENDED"}}, "unix": {"cron": {"xfer_pull": "running"},
            "spool": {"disk_pct": 41}, "config": {"/etc/xfer.conf": "0666"}}, "portal": {"sessions": 120}}


def prop(*ops, root="mainframe", fc="batch_abort"):
    out = {"root_system": root, "fault_class": fc, "hypothesis": "NIGHTBAT abended on a poison record.",
           "proposed_ops": [{"system": s, "op": o, "why": "", "params": [{"name": k, "value": v} for k, v in p.items()]}
                            for s, o, p in ops]}
    return so.normalize(out)


GOOD = [("mainframe", "quarantine_record", {"record_id": "TX000042"}), ("mainframe", "restart_batch", {"job": "NIGHTBAT"}),
        ("unix", "rerun_transfer", {}), ("portal", "refresh_cache", {})]


def verdicts(res):
    return [(r["op"], r["verdict"]) for r in res["proposed_ops"]]


def test_full_agreement_and_gate():
    res = so.judge(prop(*GOOD), PLAN, "mainframe", "batch_abort", READBACK)
    assert res["agrees_with_policy"] and res["root_agrees"] and not res["missed_ops"]
    assert all(v == "agree" for _, v in verdicts(res))
    assert [r["gated"] for r in res["proposed_ops"]] == [False, True, False, False]


def test_extra_ops_overruled_with_readback_reasons():
    res = so.judge(prop(*GOOD, ("unix", "restart_cron", {}), ("portal", "flush_sessions", {})),
                   PLAN, "mainframe", "batch_abort", READBACK)
    assert not res["agrees_with_policy"]
    bad = {o["op"]: o["reason"] for o in res["overruled_ops"]}
    assert "already running" in bad["restart_cron"]
    assert "120 sessions" in bad["flush_sessions"]


def test_fixed_param_and_wrong_record():
    res = so.judge(prop(("mainframe", "quarantine_record", {"record_id": "TX999"}), ("unix", "restart_cron", {"job": "transfer"})),
                   PLAN, "mainframe", "batch_abort", READBACK)
    bad = {o["op"]: o["reason"] for o in res["overruled_ops"]}
    assert "TX999 isn't a poison record" in bad["quarantine_record"] and "TX000042" in bad["quarantine_record"]
    assert "only xfer_pull" in bad["restart_cron"]


def test_order_and_missing_prerequisite():
    out_of_order = [GOOD[1], GOOD[0], GOOD[2], GOOD[3]]
    res = so.judge(prop(*out_of_order), PLAN, "mainframe", "batch_abort", READBACK)
    assert verdicts(res)[0] == ("restart_batch", "overruled")
    assert res["proposed_ops"][0]["reason"].startswith("out of order: poison record still queued")
    assert [v for _, v in verdicts(res)[1:]] == ["agree", "agree", "agree"]  # one mistake, one cross
    assert [m["op"] for m in res["missed_ops"]] == ["restart_batch"]
    res = so.judge(prop(GOOD[1], GOOD[3]), PLAN, "mainframe", "batch_abort", READBACK)
    assert [r["reason"].split(":")[0] for r in res["overruled_ops"]] == ["quarantine_record missing", "rerun_transfer missing"]


def test_missed_unknown_duplicate_and_malformed():
    p = prop(GOOD[0], ("mainframe", "restart_batch", {}), ("mainframe", "reboot_lpar", {}), ("unix", "refresh_cache", {}),
             ("mainframe", "quarantine_record", {"record_id": "TX000042"}), ("mainframe", "restart_batch", {"color": "x"}),
             root="unix", fc="other")
    p["ops"].append({"system": "unix", "op": "rerun_transfer", "params": {}, "why": "", "dup_params": True})
    res = so.judge(p, PLAN, "mainframe", "batch_abort", READBACK)
    rows = res["proposed_ops"]
    assert rows[1]["verdict"] == "agree" and rows[1]["note"] == "params completed by policy"
    assert "no op 'reboot_lpar'" in rows[2]["reason"]
    assert rows[3]["reason"] == "refresh_cache runs on portal, not unix"
    assert rows[4]["reason"].startswith("duplicate")
    assert rows[5]["reason"].startswith("unknown parameter color")
    assert rows[6]["reason"].startswith("malformed")
    assert not res["root_agrees"] and {m["op"] for m in res["missed_ops"]} == {"rerun_transfer", "refresh_cache"}


def test_normalize_aliases_and_caps():
    n = so.normalize({"root_system": " Mainframe ", "fault_class": "BATCH_ABORT", "hypothesis": "x" * 999,
                      "proposed_ops": [{"system": "", "op": "Unix.Restart_Cron", "params": [], "why": "w"}] * 20})
    assert n["root_system"] == "mainframe" and n["fault_class"] == "batch_abort" and len(n["hypothesis"]) == 300
    assert len(n["ops"]) == so.MAX_OPS and n["ops"][0]["system"] == "unix" and n["ops"][0]["op"] == "restart_cron"


# ---------------------------------------------------------------------------------------------- thread
class FakeSystems:
    def __init__(self, timeout=3.0):
        pass

    def ops(self, s):
        return so.static_catalog()[s]

    def state(self, s):
        return READBACK[s]

    def close(self):
        pass


@pytest.fixture()
def inc(db, monkeypatch):
    reset_runtime()
    monkeypatch.setattr(so, "Systems", FakeSystems)
    doc = {"_id": "INC-0099", "active": True, "status": "fixing", "system": "portal", "opened_at": now(),
           "symptoms": ["portal degraded, blames unix"], "affected": ["portal", "unix", "mainframe"]}
    col("incidents").insert_one(doc)
    state = {"path": ["portal", "unix", "mainframe"], "findings": {"mainframe": {
        "status": "up", "blames": None, "facts": ["batch_status=ABENDED"], "fault_class": "batch_abort",
        "root_cause": "SECRET-CONCLUSION"}}}
    yield doc, state
    col("incidents").delete_many({"_id": "INC-0099"})


def wait_ai(status, timeout=5):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        d = col("incidents").find_one({"_id": "INC-0099"}) or {}
        if (d.get("ai") or {}).get("status") == status:
            return d["ai"]
        time.sleep(0.05)
    raise AssertionError(f"ai.status never became {status}: {d.get('ai')}")


def test_no_key_writes_nothing(inc):
    doc, state = inc
    assert so.start("scout-1", doc, state, PLAN, "mainframe", "batch_abort") is False
    time.sleep(0.2)
    assert "ai" not in col("incidents").find_one({"_id": "INC-0099"})


def test_offline_when_llm_returns_none(inc, monkeypatch):
    doc, state = inc
    monkeypatch.setattr(so.llm, "llm_available", lambda: True)
    monkeypatch.setattr(so.llm, "complete_json", lambda *a, **k: None)
    assert so.start("scout-1", doc, state, PLAN, "mainframe", "batch_abort")
    ai = wait_ai("offline")
    assert ai["latency_ms"] >= 0 and ai["error"]


def test_done_path_writes_verdict_and_thought(inc, monkeypatch):
    doc, state = inc
    seen = {}

    def fake(system, prompt, schema, **k):
        seen["prompt"] = prompt
        return {"root_system": "mainframe", "fault_class": "batch_abort", "hypothesis": "Poison record.",
                "proposed_ops": [{"system": s, "op": o, "why": "", "params": [{"name": a, "value": b} for a, b in p.items()]}
                                 for s, o, p in GOOD + [("portal", "flush_sessions", {})]]}
    monkeypatch.setattr(so.llm, "llm_available", lambda: True)
    monkeypatch.setattr(so.llm, "complete_json", fake)
    assert so.start("scout-1", doc, state, PLAN, "mainframe", "batch_abort")
    ai = wait_ai("done")
    assert ai["root_agrees"] and not ai["agrees_with_policy"]
    assert [o["op"] for o in ai["overruled_ops"]] == ["flush_sessions"]
    assert "SECRET-CONCLUSION" not in seen["prompt"] and "batch_status=ABENDED" in seen["prompt"]
    t = col("thoughts").find_one({"incident": "INC-0099", "agent": "scout-1"})
    assert t and "overruled 1 of 5" in t["text"]
    # second start on the same incident is a no-op (one call per incident)
    assert so.start("scout-1", doc, state, PLAN, "mainframe", "batch_abort")
    time.sleep(0.3)
    assert col("incidents").find_one({"_id": "INC-0099"})["ai"]["request_id"] == ai["request_id"]


def test_param_values_are_case_exact():
    res = so.judge(prop(GOOD[0], ("mainframe", "restart_batch", {"job": "nightbat"}), *GOOD[2:]),
                   PLAN, "mainframe", "batch_abort", READBACK)
    assert verdicts(res)[1] == ("restart_batch", "overruled") and not res["agrees_with_policy"]


def test_order_follows_the_plan_chain_for_other_faults():
    plan = [dict(s, n=i) for i, s in enumerate(policy.fix_plan("disk_fill"), 1)]
    p = prop(("unix", "rerun_transfer", {}), ("unix", "purge_spool", {"path": "/var/spool/xfer"}), ("portal", "refresh_cache", {}),
             root="unix", fc="disk_fill")
    res = so.judge(p, plan, "unix", "disk_fill", READBACK)
    assert res["proposed_ops"][0]["reason"] == "out of order: spool still full: the transfer would fail again"
    assert not res["agrees_with_policy"]
