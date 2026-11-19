"""Schema drift adaptation without agent processes: real systems app (TestClient) + local MongoDB.

detect (MongoDB $jsonSchema + field-diff aggregation) -> fix plan -> Verifier read-back -> learned mapping v2,
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
    from agents import diagnosis, policy, schema, verify
    c = estate
    read = lambda s: c.get(f"/{s}/state").json()  # noqa: E731

    # healthy feed: the monitor keeps a good sample, diagnosis sees no drift
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

    # the Verifier: not applied until the portal really reads the new field
    ok, ev = verify.check("map_field", plan[0]["params"], read)
    assert not ok and "map.balance=balance" in ev
    assert _op(c, "portal", "map_field", plan[0]["params"])["applied"]
    ok, ev = verify.check("map_field", plan[0]["params"], read)
    assert ok, ev
    assert "line up" in ev
    m = schema.learn("balance", "acct_bal", "INC-T", "INC-T-s1", ev, "verifier-1")
    assert m["version"] == 2 and m["from"] == "balance"
    assert schema.learn("balance", "acct_bal", "INC-T", "INC-T-s1", ev, "verifier-1") is None   # idempotent
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
    from agents import diagnosis
    c = estate
    f = diagnosis.classify("unix", c.get("/unix/health").json(), c.get("/unix/metrics").json(), [],
                           c.get("/unix/state").json())
    assert f["fault_class"] is None


def test_line_up_is_a_units_check():
    from agents import schema
    good = [{"acct": "1", "balance": 1_000_000}, {"acct": "2", "balance": 500_000}]
    assert schema.line_up(good, "balance", [{"acct": "1", "acct_bal": 940_000}, {"acct": "2", "acct_bal": 530_000}],
                          "acct_bal") == (2, 2)                                   # postings moved balances a bit
    assert schema.line_up(good, "balance", [{"acct": "1", "usd": 10_000}, {"acct": "2", "usd": 5_000}],
                          "usd") == (0, 2)                                        # dollars, not cents
    assert schema.line_up(good, "balance", [{"acct": "9", "acct_bal": 1}], "acct_bal") == (0, 0)


@pytest.fixture()
def drifted(estate, db, monkeypatch):
    """Good sample stored, then the manifest drifts; Claude is faked (no key needed)."""
    from agents import schema
    db.playbooks.delete_many({"fault_class": "schema_drift"})
    read = lambda s: estate.get(f"/{s}/state").json()  # noqa: E731
    assert schema.capture_good(read("unix"))
    _drift_and_break(estate)
    monkeypatch.setenv("AI_SECOND_OPINION", "1")
    monkeypatch.setattr(schema.llm, "llm_available", lambda: True)
    return read


def _answer(monkeypatch, text, delay=0.0):
    import time as _t
    from agents import schema
    calls = []

    def fake(system, prompt, max_tokens=200):
        calls.append(prompt)
        _t.sleep(delay)
        return text
    monkeypatch.setattr(schema.llm, "complete", fake)
    return calls


def test_claude_proposes_and_rules_precheck(drifted, monkeypatch):
    from agents import schema
    calls = _answer(monkeypatch, 'Sure: {"source": "acct_bal", "hypothesis": "acct_bal is the renamed balance", '
                                 '"confidence": 0.93, "cites": ["old.balance", "new.acct_bal"]}')
    f = schema.detect(drifted("unix"))
    assert len(calls) == 1 and "acct_bal" in calls[0]
    p = f["params"]
    assert p["to"] == "acct_bal" and p["proposed_by"] == "claude"
    ai = p["ai"]
    assert ai["kind"] == "schema_mapping" and ai["confidence"] == 0.93 and ai["proposed_ops"][0]["op"] == "map_field"
    assert ai["verdicts"][0]["verdict"] == "accepted" and ai["verifier"] == {"verdict": "pending"}
    assert any(x.startswith("Claude proposed balance → acct_bal") for x in f["facts"])


def test_claude_overruled_and_timeout_fall_back_to_rules(drifted, monkeypatch):
    from agents import schema
    _answer(monkeypatch, '{"source": "as_of", "hypothesis": "maybe", "confidence": 0.4}')
    f = schema.detect(drifted("unix"))
    assert f["params"]["to"] == "acct_bal" and f["params"]["proposed_by"] == "rules"
    assert f["params"]["ai"]["verdicts"][0]["verdict"] == "overruled"

    monkeypatch.setattr(schema, "AI_TIMEOUT_S", 0.2)
    _answer(monkeypatch, '{"source": "acct_bal"}', delay=1.0)
    f = schema.detect(drifted("unix"))
    assert f["params"]["to"] == "acct_bal" and f["params"]["ai"]["status"] == "offline"
    assert "timeout" in f["params"]["ai"]["error"]


def test_memory_skips_claude_and_verifier_stamps_incident(drifted, db, monkeypatch):
    from common.db import now
    from agents import schema
    db.playbooks.insert_one({"_id": "PB-T1", "fault_class": "schema_drift", "stale": False, "created_at": now(),
                             "fix": [{"system": "portal", "op": "map_field",
                                      "params": {"field": "balance", "source": "acct_bal"}}]})
    calls = _answer(monkeypatch, '{"source": "acct_bal"}')
    f = schema.detect(drifted("unix"))
    assert calls == [] and f["params"]["proposed_by"] == "memory" and f["params"]["ai"]["cites"] == ["PB-T1"]
    db.incidents.delete_many({"_id": "INC-AI"})
    db.incidents.insert_one({"_id": "INC-AI", "active": False, "status": "resolved", "system": "portal", "opened_at": now()})
    schema.record_ai("INC-AI", f["params"]["ai"])
    doc = schema.verifier_verdict("INC-AI", "acct_bal", "values line up with good sample: 3/3 accounts", 2)
    assert doc["ai"]["verifier"]["verdict"] == "verified" and doc["ai"]["verifier"]["reason"].startswith("3/3 values match")
    db.playbooks.delete_many({"_id": "PB-T1"})


def test_no_flag_no_ai_field(estate, db, monkeypatch):
    from agents import schema
    read = lambda s: estate.get(f"/{s}/state").json()  # noqa: E731
    db.playbooks.delete_many({"fault_class": "schema_drift"})
    schema.capture_good(read("unix"))
    _drift_and_break(estate)
    monkeypatch.delenv("AI_SECOND_OPINION", raising=False)
    f = schema.detect(read("unix"))
    assert f["params"]["proposed_by"] == "rules" and "ai" not in f["params"]



def _fix(c, read, f, inc):
    """What the operator + Verifier do: map_field, Verifier read-back, learn (re-capturing the good sample), refresh."""
    from agents import policy, schema, verify
    plan = policy.fix_plan("schema_drift", f["params"])
    assert _op(c, "portal", "map_field", plan[0]["params"])["applied"]
    ok, ev = verify.check("map_field", plan[0]["params"], read)
    assert ok, ev
    m = schema.learn_verified("balance", f["params"]["to"], inc, f"{inc}-s1", ev, "verifier-1", read("unix"))
    assert _op(c, "portal", "refresh_cache")["applied"]
    c.post("/_admin/tick?n=3")
    assert c.get("/portal/health").json()["status"] == "up"
    return m


@pytest.fixture()
def clean(estate, db, monkeypatch):
    from agents import schema
    db.playbooks.delete_many({"fault_class": "schema_drift"})
    monkeypatch.delenv("AI_SECOND_OPINION", raising=False)
    read = lambda s: estate.get(f"/{s}/state").json()  # noqa: E731
    assert schema.capture_good(read("unix"))
    return estate, read


def test_redrift_right_after_a_fix_still_adapts(clean):
    """Review fix 1: the Verifier re-captures the good sample right after learning, so a second drift fired
    before the monitor's next 10 s sample is still diffed (acct_bal -> ledger_bal) and verified (v3)."""
    from agents import diagnosis, policy, schema, verify
    c, read = clean
    _drift_and_break(c)
    m = _fix(c, read, schema.detect(read("unix")), "INC-R1")
    assert m["version"] == 2 and not m["reused"]
    assert isinstance(schema.good_sample()["record"].get("acct_bal"), (int, float))   # no stale-sample window
    _drift_and_break(c)                                       # no monitor capture in between
    f3 = diagnosis.classify("unix", c.get("/unix/health").json(), c.get("/unix/metrics").json(), [], read("unix"))
    assert f3["params"]["from"] == "acct_bal" and f3["params"]["to"] == "ledger_bal" and f3["params"]["version"] == 3
    plan = policy.fix_plan("schema_drift", f3["params"])
    assert _op(c, "portal", "map_field", plan[0]["params"])["applied"]
    ok, ev = verify.check("map_field", plan[0]["params"], read)
    assert ok and "line up with good sample: 3/3" in ev, ev


def test_memory_value_check_marks_stale_and_falls_back(clean, db):
    """Review fix 3: a remembered mapping whose values don't line up is marked stale; rules propose instead."""
    from common.db import now
    from agents import schema
    c, read = clean
    _drift_and_break(c)
    st = read("unix")
    for r in st["feed"]["sample"]:      # a decoy new numeric field in the wrong units (dollars, not cents)
        r["usd"] = r["acct_bal"] // 100
    db.playbooks.insert_one({"_id": "PB-BAD", "fault_class": "schema_drift", "stale": False, "created_at": now(),
                             "fix": [{"system": "portal", "op": "map_field", "params": {"source": "usd"}}]})
    f = schema.detect(st)
    assert f["params"]["proposed_by"] == "rules" and f["params"]["to"] == "acct_bal"
    assert any("marked stale" in x for x in f["facts"])
    assert db.playbooks.find_one({"_id": "PB-BAD"})["stale"] is True
    db.playbooks.delete_many({"_id": "PB-BAD"})


def test_mapping_survives_a_round_and_is_reused(clean, db):
    """Durable memory: a round reset rewinds the live mapping to v1 but keeps the verified history, so the same
    vendor patch next night is "seen before" (no Claude call) and the Verifier reuses v2."""
    from agents import schema
    c, read = clean
    _drift_and_break(c)
    _fix(c, read, schema.detect(read("unix")), "INC-N1")
    # what /api/reset next_round does to schema_mappings (console/api.py), then the estate is back at v1
    db.schema_mappings.update_many({"$expr": {"$ne": ["$source", "$field"]}},
                                   [{"$set": {"previous": "$source", "source": "$field", "version": 1}}])
    db.schema_samples.delete_many({})
    assert c.post("/_admin/reset").status_code == 200
    c.post("/_admin/tick?n=3")
    assert schema.detect(read("unix")) is None                 # v1 feed conforms to the rewound mapping
    assert schema.capture_good(read("unix"))
    _drift_and_break(c)
    f = schema.detect(read("unix"))
    p = f["params"]
    assert p["proposed_by"] == "memory" and p["to"] == "acct_bal" and p["version"] == 2 and p["reused_version"] == 2
    assert f["facts"][0].startswith("seen before: mapping v2 reused (balance → acct_bal, verified on INC-N1")
    m = _fix(c, read, f, "INC-N2")
    assert m["version"] == 2 and m["reused"] is True
    doc = db.schema_mappings.find_one({"_id": "unix.xfer_manifest.balance"})
    assert doc["source"] == "acct_bal" and doc["version"] == 2 and doc["reused"] is True and len(doc["history"]) == 2


def test_verifier_provenance_names_the_proposer(db):
    """Review fix 2: the Verifier's chain line names who proposed the mapping, never "no playbook… → memory"."""
    from agents.roles.verifier import provenance
    m = {"from": "balance", "to": "acct_bal", "version": 2}
    mem = provenance("INC-X", {**m, "reused": True}, {"proposed_by": "memory"}, "3/3 values match")
    assert mem.startswith("memory: MongoDB already knew balance→acct_bal") and "no playbook" not in mem
    assert mem.endswith("mapping v2 reused.")
    cl = provenance("INC-X", m, {"proposed_by": "claude", "verdicts": [{"verdict": "accepted"}]}, "3/3 values match")
    assert "Claude proposed balance→acct_bal → Verifier verified (3/3 values match) → mapping v2 stored." in cl
    ov = provenance("INC-X", m, {"proposed_by": "claude", "verdicts": [{"verdict": "overruled"}],
                                 "proposed_ops": [{"params": {"source": "as_of"}}]}, "3/3 values match")
    assert "Claude proposed balance→as_of, rules overruled → acct_bal" in ov
    assert provenance("INC-X", m, {}, "values line up").startswith("rules: no playbook/op for unknown field acct_bal")


def test_feed_change_for_the_monitor(clean):
    from agents import schema
    c, read = clean
    assert schema.feed_change(read("unix")) is None
    _drift_and_break(c)
    assert schema.feed_change(read("unix")) == (["balance"], ["acct_bal"])
