"""Mainframe contract tests. Must pass BEFORE and AFTER the Gardener's validate_record patch.

They check the API surface, receipts and honesty rules — not "a malformed record ABENDs"
(that is exactly what the Gardener's upgrade changes; see test_cascade.py).
"""
from datetime import datetime

import systems.mainframe as mf
from systems.tests.helpers import chaos, op, status, tick

GOOD = mf.format_record("TX000042", "20261114", "4211000101", "4211000102", 12345, memo="ACH TRANSFER")


def test_validate_record_signature_and_good_records():
    # Whatever the body (legacy or patched), well-formed records must be accepted.
    assert mf.validate_record(GOOD) is True
    for signed in ("+00000012345", "00000012345-"):
        rec = GOOD[:mf.AMOUNT_FIELD.start] + signed + GOOD[mf.AMOUNT_FIELD.stop:]
        assert len(rec) == mf.RECORD_LEN and mf.validate_record(rec) is True
    assert len(mf.BAD_SAMPLE_RECORD) == mf.RECORD_LEN
    assert mf.is_malformed(mf.BAD_SAMPLE_RECORD) and not mf.is_malformed(GOOD)


def test_health_metrics_shape(c):
    assert c.get("/mainframe/health").json() == {"system": "mainframe", "status": "up", "blames": None, "version": 1}
    m = c.get("/mainframe/metrics").json()
    for k in ("system", "version", "latency_ms", "error_rate", "queue_depth", "disk_pct", "last_batch_ok",
              "batch_status", "sessions"):
        assert k in m
    assert m["batch_status"] == "OK"
    datetime.fromisoformat(m["last_batch_ok"])


def test_unknown_system_and_vuln_404(c):
    assert c.get("/vax/health").status_code == 404
    assert c.post("/mainframe/security/probe/nope").status_code == 404


def test_ops_catalog(c):
    ops = {o["op"]: o for o in c.get("/mainframe/ops").json()}
    assert set(ops) == {"quarantine_record", "restart_batch", "rotate_credential"}
    assert ops["restart_batch"]["destructive"] and ops["rotate_credential"]["destructive"]
    assert not ops["quarantine_record"]["destructive"]
    for o in ops.values():
        assert {"op", "params", "destructive", "desc", "postcondition"} <= set(o)


def test_state_fields_and_validation_flag(c):
    st = c.get("/mainframe/state").json()
    b = st["batch"]
    assert b["job"] == "NIGHTBAT" and b["status"] == "OK"
    assert b["poison_records"] == [] and b["quarantined"] == []
    assert st["credentials"] == {"OPERATOR": "default"}
    assert st["version"] == 1
    assert st["validation_enabled"] is (mf.validate_record(mf.BAD_SAMPLE_RECORD) is False)


def test_batch_runs_on_ticks_and_logs(c):
    before = c.get("/mainframe/state").json()["batch"]["last_run_ok"]
    tick(c, 4)
    after = c.get("/mainframe/state").json()["batch"]["last_run_ok"]
    assert after > before
    lines = c.get("/mainframe/logs?limit=50").json()["lines"]
    assert any("$HASP" in ln["line"] for ln in lines)
    assert all(set(ln) == {"ts", "level", "code", "line"} for ln in lines)
    # since= filters, limit= caps
    last = lines[-1]["ts"]
    assert c.get(f"/mainframe/logs?since={last}").json()["lines"] == []
    assert len(c.get("/mainframe/logs?limit=1").json()["lines"]) == 1


def test_batch_handles_malformed_record_per_current_validation(c):
    chaos(c, "mainframe", "batch_abort")
    tick(c, 3)
    b = c.get("/mainframe/state").json()["batch"]
    if c.get("/mainframe/state").json()["validation_enabled"]:
        assert b["status"] == "OK" and b["rejected"] and not b["poison_records"]
    else:
        assert b["status"] == "ABENDED" and b["poison_records"]
    assert status(c, "mainframe")[0] == "up"  # online region stays up either way


def test_receipt_idempotency(c, wdb):
    r1 = op(c, "mainframe", "rotate_credential", {"account": "OPERATOR"}, action_id="INC-T-s1-a1")
    r2 = op(c, "mainframe", "rotate_credential", {"account": "OPERATOR"}, action_id="INC-T-s1-a1")
    assert r1["applied"] and r2["applied"] and r2.get("duplicate")
    assert r1["action_id"] == r2["action_id"] == "INC-T-s1-a1"
    assert wdb.op_receipts.count_documents({"_id": "INC-T-s1-a1"}) == 1
    assert c.get("/mainframe/state").json()["credentials"]["OPERATOR"] == "rotated"
    p = c.post("/mainframe/security/probe/mf-default-operator").json()
    assert p["vuln_id"] == "mf-default-operator" and p["exploitable"] is False
    assert c.post("/mainframe/security/probe/default_credential").json()["exploitable"] is False  # by kind


def test_lying_success_acks_without_applying(c, wdb):
    chaos(c, "mainframe", "lying_success", {"count": 1})
    r = op(c, "mainframe", "rotate_credential", {"account": "OPERATOR"}, action_id="lie-1")
    assert r["accepted"] and r["applied"]                      # the ACK...
    assert c.get("/mainframe/state").json()["credentials"]["OPERATOR"] == "default"  # ...is not APPLIED
    rec = wdb.op_receipts.find_one({"_id": "lie-1"})
    assert rec["lied"] is True and rec["effective"] is False and rec["claimed_applied"] is True
    r = op(c, "mainframe", "rotate_credential", {"account": "OPERATOR"}, action_id="lie-1-a2")
    assert r["applied"] and c.get("/mainframe/state").json()["credentials"]["OPERATOR"] == "rotated"


def test_slow_apply_lands_on_a_later_tick(c, wdb):
    chaos(c, "mainframe", "slow_apply", {"delay_s": 0})
    r = op(c, "mainframe", "rotate_credential", {"account": "OPERATOR"}, action_id="slow-1")
    assert r["accepted"] and r["applied"] is False
    assert c.get("/mainframe/state").json()["credentials"]["OPERATOR"] == "default"
    assert wdb.op_receipts.find_one({"_id": "slow-1"})["apply_at"] is not None
    tick(c)
    assert c.get("/mainframe/state").json()["credentials"]["OPERATOR"] == "rotated"
    again = op(c, "mainframe", "rotate_credential", {"account": "OPERATOR"}, action_id="slow-1")
    assert again["applied"] is True and again.get("duplicate")


def test_bad_params_and_unknown_op(c):
    r = op(c, "mainframe", "quarantine_record", {"record_id": "TX404404"})
    assert r["accepted"] and r["applied"] is False
    r = op(c, "mainframe", "format_dasd", {})
    assert r["accepted"] is False


def test_source_and_reload(c):
    src = c.get("/mainframe/source").json()
    assert "def validate_record(record: str) -> bool:" in src["source"]
    assert src["path"] == "systems/mainframe.py"
    r = c.post("/_admin/reload/mainframe").json()
    assert r["version_after"] == r["version_before"] + 1
    assert c.get("/mainframe/health").json()["version"] == r["version_after"]
    assert c.get("/mainframe/state").json()["version"] == r["version_after"]
