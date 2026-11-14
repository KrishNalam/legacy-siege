"""Every Red attack that had no swarm-side catch test (results/W28-matrix.md): fire the REAL fault on the real
systems app (in-process, driven by ticks) and assert the Hive's own decision code catches it — the
specialist's diagnosis (swarm.diagnosis.classify), the deterministic plan (swarm.policy.fix_plan) and the
Guard's read-back (swarm.verify.check). No swarm processes: fast and hermetic, so each attack stays covered."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from swarm import policy, verify
from swarm.diagnosis import classify

ALL = ("portal", "unix", "mainframe")


@pytest.fixture(scope="module")
def c(db):
    from systems.app import create_app
    with TestClient(create_app(tick_s=0)) as tc:
        yield tc


@pytest.fixture(autouse=True)
def fresh(c):
    assert c.post("/_admin/reset").status_code == 200


def tick(c, n=1):
    assert c.post(f"/_admin/tick?n={n}").status_code == 200


def chaos(c, sys, fault, params=None):
    r = c.post(f"/{sys}/_chaos/{fault}", json={"params": params or {}})
    assert r.status_code == 200 and r.json()["ok"], r.text
    return r.json()


def probe(c):
    return lambda s, vid: c.post(f"/{s}/security/probe/{vid}").json()


def logs(c, sys):
    return c.get(f"/{sys}/logs?limit=50").json()["lines"]


def look(c, sys) -> dict:
    """What a specialist worker concludes from one hop (health, metrics, logs, state, probe)."""
    return classify(sys, c.get(f"/{sys}/health").json(), c.get(f"/{sys}/metrics").json(), logs(c, sys),
                    c.get(f"/{sys}/state").json(), probe=probe(c))


def send(c, step) -> dict:
    r = c.post(f"/{step['system']}/ops/{step['op']}",
               json={"action_id": f"t-{uuid.uuid4().hex[:10]}", "params": step.get("params") or {}})
    assert r.status_code == 200, r.text
    return r.json()


def guard(c, step) -> tuple[bool, str]:
    return verify.check(step["op"], step.get("params") or {}, lambda s: c.get(f"/{s}/state").json(), probe=probe(c))


def fix_and_verify(c, fault_class, params=None):
    """Run the policy plan in order; the Guard must read each step back as applied."""
    plan = policy.fix_plan(fault_class, params)
    assert plan, f"no plan for {fault_class}"
    for st in plan:
        send(c, st)
        tick(c, 2)
        ok, evidence = guard(c, st)
        assert ok, f"{st['op']} not applied: {evidence}"
    tick(c, 2)
    return plan


def only(c, sys, fault_class) -> dict:
    """Exactly `sys` is diagnosed with `fault_class`; every other hop finds nothing."""
    found = {s: look(c, s) for s in ALL}
    assert found[sys]["fault_class"] == fault_class, found[sys]
    assert all(found[s]["fault_class"] is None for s in ALL if s != sys), found
    return found[sys]


# ------------------------------------------------------------------ G1 session_leak (Saboteur)
def test_session_leak_caught_and_flushed(c):
    chaos(c, "portal", "session_leak")
    for _ in range(40):  # leaks per tick until the store is exhausted
        tick(c)
        if c.get("/portal/health").json()["status"] != "up":
            break
    assert c.get("/portal/health").json()["status"] == "degraded"   # the Sentinel's trigger
    f = only(c, "portal", "session_leak")
    assert any(fact.startswith("sessions=") for fact in f["facts"])
    plan = fix_and_verify(c, "session_leak")
    assert [s["op"] for s in plan] == ["flush_sessions"] and not plan[0]["destructive"]
    assert c.get("/portal/health").json()["status"] == "up"
