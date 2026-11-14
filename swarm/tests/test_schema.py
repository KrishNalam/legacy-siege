"""Schema drift adaptation without agent processes: real systems app (TestClient) + local MongoDB.

detect (MongoDB $jsonSchema + field-diff aggregation) -> fix plan -> Guard read-back -> learned mapping v2,
then a second drift is diffed against the LEARNED schema (v2 -> v3)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def estate(db):
    from systems.app import create_app
    for n in ("schema_samples", "schema_mappings"):
        db[n].delete_many({})
    with TestClient(create_app(tick_s=0)) as tc:
        assert tc.post("/_admin/reset").status_code == 200
        tc.post("/_admin/tick?n=3")
        yield tc


def _op(c, sys, name, params=None, n=[0]):
    n[0] += 1
    return c.post(f"/{sys}/ops/{name}", json={"action_id": f"schema-test-{n[0]}", "params": params or {}}).json()


def _drift_and_break(c):
    assert c.post("/unix/_chaos/schema_drift", json={"params": {}}).json()["ok"]
    for _ in range(20):
        c.post("/_admin/tick")
        if c.get("/portal/health").json()["status"] == "degraded":
            return
    raise AssertionError("portal never degraded")


def test_detect_verify_learn_and_redrift(estate, db):
    from swarm import diagnosis, policy, schema, verify
    c = estate
    read = lambda s: c.get(f"/{s}/state").json()  # noqa: E731

    # healthy feed: the sentinel keeps a good sample, diagnosis sees no drift
    assert schema.capture_good(read("unix")) is True
    assert schema.detect(read("unix")) is None

    _drift_and_break(c)
    assert schema.capture_good(read("unix")) is False          # a drifted sample never becomes "good"
    f = diagnosis.classify("unix", c.get("/unix/health").json(), c.get("/unix/metrics").json(), [], read("unix"))
    assert f["fault_class"] == "schema_drift" and f["status"] == "up"
    assert f["params"]["from"] == "balance" and f["params"]["to"] == "acct_bal"
    assert f["params"]["lined_up"][0] == f["params"]["lined_up"][1] > 0
    assert "balance → acct_bal" in f["root_cause"]
    assert db.schema_samples.count_documents({"kind": "observed", "good": False}) == 1

    plan = policy.fix_plan("schema_drift", f["params"])
    assert [(s["system"], s["op"]) for s in plan] == [("portal", "map_field"), ("portal", "refresh_cache")]
    assert policy.rebind([{"system": "portal", "op": "map_field", "params": {"source": "stale_name"}}],
                         "schema_drift", f["params"])[0]["params"]["source"] == "acct_bal"

    # the Guard: not applied until the portal really reads the new field
    ok, ev = verify.check("map_field", plan[0]["params"], read)
    assert not ok and "map.balance=balance" in ev
    assert _op(c, "portal", "map_field", plan[0]["params"])["applied"]
    ok, ev = verify.check("map_field", plan[0]["params"], read)
    assert ok, ev
    assert "line up" in ev
    m = schema.learn("balance", "acct_bal", "INC-T", "INC-T-s1", ev, "guard-1")
    assert m["version"] == 2 and m["from"] == "balance"
    assert schema.learn("balance", "acct_bal", "INC-T", "INC-T-s1", ev, "guard-1") is None   # idempotent
    doc = db.schema_mappings.find_one({"_id": "unix.xfer_manifest.balance"})
    assert doc["source"] == "acct_bal" and doc["version"] == 2 and len(doc["history"]) == 1
    assert _op(c, "portal", "refresh_cache")["applied"]
    c.post("/_admin/tick?n=3")
    assert c.get("/portal/health").json()["status"] == "up"

    # every agent now expects acct_bal: the healthy v2 feed conforms and becomes the good sample
    assert schema.expected_fields()["acct_bal"] == "number"
    assert schema.detect(read("unix")) is None
    assert schema.capture_good(read("unix")) is True

    # a second vendor patch: diffed against the learned schema -> v3
    _drift_and_break(c)
    f3 = diagnosis.classify("unix", c.get("/unix/health").json(), c.get("/unix/metrics").json(), [], read("unix"))
    assert f3["params"]["from"] == "acct_bal" and f3["params"]["to"] == "ledger_bal" and f3["params"]["version"] == 3


def test_drift_check_never_breaks_batch_abort_diagnosis(estate):
    """A normal unix hop (no feed change) still classifies as before."""
    from swarm import diagnosis
    c = estate
    f = diagnosis.classify("unix", c.get("/unix/health").json(), c.get("/unix/metrics").json(), [],
                           c.get("/unix/state").json())
    assert f["fault_class"] is None


def test_line_up_is_a_units_check():
    from swarm import schema
    good = [{"acct": "1", "balance": 1_000_000}, {"acct": "2", "balance": 500_000}]
    assert schema.line_up(good, "balance", [{"acct": "1", "acct_bal": 940_000}, {"acct": "2", "acct_bal": 530_000}],
                          "acct_bal") == (2, 2)                                   # postings moved balances a bit
    assert schema.line_up(good, "balance", [{"acct": "1", "usd": 10_000}, {"acct": "2", "usd": 5_000}],
                          "usd") == (0, 2)                                        # dollars, not cents
    assert schema.line_up(good, "balance", [{"acct": "9", "acct_bal": 1}], "acct_bal") == (0, 0)
