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
           "cites": ["E1"], "confidence": 0.8,
           "proposed_ops": [{"target": s, "op": o, "why": "", "risk": "destructive" if o == "restart_batch" else "safe",
                             "params": [{"name": k, "value": v} for k, v in p.items()]} for s, o, p in ops]}
    return so.normalize(out)


GOOD = [("mainframe", "quarantine_record", {"record_id": "TX000042"}), ("mainframe", "restart_batch", {"job": "NIGHTBAT"}),
        ("unix", "rerun_transfer", {}), ("portal", "refresh_cache", {})]


def verdicts(res):
    return [(r["op"], r["verdict"]) for r in res["verdicts"]]


def test_full_agreement_and_gate():
    res = so.judge(prop(*GOOD), PLAN, "mainframe", "batch_abort", READBACK)
    assert res["agrees_with_policy"] and res["root_agrees"] and not res["missed_ops"]
    assert all(v == "accepted" for _, v in verdicts(res))
    assert [r["gated"] for r in res["verdicts"]] == [False, True, False, False]
    assert res["verdicts"][1]["rule"] == so.P6


def test_extra_ops_overruled_with_readback_reasons():
    res = so.judge(prop(*GOOD, ("unix", "restart_cron", {}), ("portal", "flush_sessions", {})),
                   PLAN, "mainframe", "batch_abort", READBACK)
    assert not res["agrees_with_policy"]
    bad = {o["op"]: (o["rule"], o["reason"]) for o in res["verdicts"] if o["verdict"] == "overruled"}
    assert bad["restart_cron"][0] == so.P4 and "already running" in bad["restart_cron"][1]
    assert bad["flush_sessions"][0] == so.P3 and "120 sessions" in bad["flush_sessions"][1]


def test_fixed_param_and_wrong_record():
    res = so.judge(prop(("mainframe", "quarantine_record", {"record_id": "TX999"}), ("unix", "restart_cron", {"job": "transfer"})),
                   PLAN, "mainframe", "batch_abort", READBACK)
    bad = {o["op"]: o["reason"] for o in res["verdicts"] if o["verdict"] == "overruled"}
    assert "TX999 isn't a poison record" in bad["quarantine_record"] and "TX000042" in bad["quarantine_record"]
    assert "only xfer_pull" in bad["restart_cron"]


def test_order_and_missing_prerequisite():
    out_of_order = [GOOD[1], GOOD[0], GOOD[2], GOOD[3]]
    res = so.judge(prop(*out_of_order), PLAN, "mainframe", "batch_abort", READBACK)
    assert verdicts(res)[0] == ("restart_batch", "overruled")
    assert res["verdicts"][0]["rule"] == so.P2
    assert res["verdicts"][0]["reason"].startswith("runs before quarantine_record: poison record still queued")
    assert [v for _, v in verdicts(res)[1:]] == ["accepted", "accepted", "accepted"]  # one mistake, one cross
    assert [m["op"] for m in res["missed_ops"]] == ["restart_batch"]
    res = so.judge(prop(GOOD[1], GOOD[3]), PLAN, "mainframe", "batch_abort", READBACK)
    assert [r["reason"].split(":")[0] for r in res["verdicts"]] == ["skips quarantine_record", "skips rerun_transfer"]


def test_missed_unknown_duplicate_and_malformed():
    p = prop(GOOD[0], ("mainframe", "restart_batch", {}), ("mainframe", "reboot_lpar", {}), ("unix", "refresh_cache", {}),
             ("mainframe", "quarantine_record", {"record_id": "TX000042"}), ("mainframe", "restart_batch", {"color": "x"}),
             root="unix", fc="other")
    p["ops"].append({"target": "unix", "op": "rerun_transfer", "params": {}, "why": "", "risk": "safe", "dup_params": True})
    res = so.judge(p, PLAN, "mainframe", "batch_abort", READBACK)
    rows = res["verdicts"]
    assert rows[1]["verdict"] == "accepted" and rows[1]["note"] == "params completed by policy"
    assert "no op 'reboot_lpar'" in rows[2]["reason"]
    assert rows[3]["reason"] == "refresh_cache runs on portal, not unix"
    assert rows[4]["reason"].startswith("duplicate")
    assert rows[5]["reason"].startswith("unknown parameter color")
    assert rows[6]["reason"].startswith("malformed")
    assert not res["root_agrees"] and {m["op"] for m in res["missed_ops"]} == {"rerun_transfer", "refresh_cache"}


def test_normalize_aliases_and_caps():
    n = so.normalize({"root_system": " Mainframe ", "fault_class": "BATCH_ABORT", "hypothesis": "x" * 999,
                      "cites": ["e2", "E2", "E99", " E1 "], "confidence": 7,
                      "proposed_ops": [{"target": "", "op": "Unix.Restart_Cron", "params": [], "why": "w", "risk": "?"}] * 20},
                     {"E1", "E2"})
    assert n["root_system"] == "mainframe" and n["fault_class"] == "batch_abort" and len(n["hypothesis"]) == 300
    assert len(n["ops"]) == so.MAX_OPS and n["ops"][0]["target"] == "unix" and n["ops"][0]["op"] == "restart_cron"
    assert n["cites"] == ["E2", "E1"] and n["confidence"] == 1.0 and n["ops"][0]["risk"] == "safe"


def test_safe_label_on_destructive_op_is_noted():
    p = prop(*GOOD)
    p["ops"][1]["risk"] = "safe"
    res = so.judge(p, PLAN, "mainframe", "batch_abort", READBACK)
    assert res["verdicts"][1]["note"] == "Claude rated it safe; it is destructive"


# ---------------------------------------------------------------------------------------------- thread
class FakeSystems:
    def __init__(self, timeout=3.0):
        pass

    def ops(self, s):
        return so.static_catalog()[s]

    def state(self, s):
        return READBACK[s]

    def logs(self, s, limit=50):
        return [{"level": "ERROR", "code": "IEF450I", "line": "NIGHTBAT ABEND=S0C7"}] if s == "mainframe" else []

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


def test_flag_off_or_no_key_writes_nothing(inc, monkeypatch):
    doc, state = inc
    monkeypatch.setattr(so.config, "AI_SECOND_OPINION", True)  # flag on, but no key in tests
    assert so.start("scout-1", doc, state, PLAN, "mainframe", "batch_abort") is False
    monkeypatch.setattr(so.config, "AI_SECOND_OPINION", False)
    monkeypatch.setattr(so.llm, "llm_available", lambda: True)  # key, but flag off
    assert so.start("scout-1", doc, state, PLAN, "mainframe", "batch_abort") is False
    time.sleep(0.2)
    assert "ai" not in col("incidents").find_one({"_id": "INC-0099"})


def test_offline_when_llm_returns_none(inc, monkeypatch):
    doc, state = inc
    monkeypatch.setattr(so.llm, "llm_available", lambda: True)
    monkeypatch.setattr(so.config, "AI_SECOND_OPINION", True)
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
                "cites": ["E1", "E9", "E77"], "confidence": 0.8,
                "proposed_ops": [{"target": s, "op": o, "why": "", "risk": "safe", "params": [{"name": a, "value": b} for a, b in p.items()]}
                                 for s, o, p in GOOD + [("portal", "flush_sessions", {})]]}
    monkeypatch.setattr(so.llm, "llm_available", lambda: True)
    monkeypatch.setattr(so.config, "AI_SECOND_OPINION", True)
    monkeypatch.setattr(so.llm, "complete_json", fake)
    assert so.start("scout-1", doc, state, PLAN, "mainframe", "batch_abort")
    ai = wait_ai("done")
    assert ai["kind"] == "second_opinion" and ai["root_agrees"] and not ai["agrees_with_policy"]
    assert len(ai["verdicts"]) == len(ai["proposed_ops"]) == 5
    assert [v["op"] for v in ai["verdicts"] if v["verdict"] == "overruled"] == ["flush_sessions"]
    assert "SECRET-CONCLUSION" not in seen["prompt"] and "batch_status=ABENDED" in seen["prompt"]
    assert "NIGHTBAT ABEND=S0C7" in seen["prompt"]  # last log lines are evidence too
    assert ai["cites"] == ["E1"] or set(ai["cites"]) <= {e["id"] for e in ai["evidence"]}
    assert "E77" not in ai["cites"] and ai["confidence"] == 0.8
    d = col("incidents").find_one({"_id": "INC-0099"})["ai"]
    assert d["tally"]["proposed"] >= 5 and d["tally"]["overruled"] >= 1
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
    assert res["verdicts"][0]["reason"] == "runs before purge_spool: spool still full: the transfer would fail again"
    assert not res["agrees_with_policy"]
