"""Demo autoplay (DEMO_AUTOPLAY): approvals after the first auto-approve; nights advance once, in order."""
from __future__ import annotations

from datetime import timedelta

from common.db import now, swarm, world
from warroom import api
from warroom.autoplay import Autoplay


def _apr(_id, desc="restart_batch NIGHTBAT on mainframe", status="pending", age=10, kind="step", **kw):
    swarm().approvals.insert_one({"_id": _id, "kind": kind, "desc": desc, "status": status, "ts": now() - timedelta(seconds=age),
                                  "decided_by": "beekeeper" if status == "approved" else None, **kw})


def _estate(status="up"):
    for s in ("mainframe", "unix", "portal"):
        world().systems.replace_one({"_id": s}, {"_id": s, "status": status}, upsert=True)


def _night(scenario, run_id, closed_age=10, status="resolved", active=False):
    swarm().stats.replace_one({"_id": "current"}, {"_id": "current", "run_id": run_id, "scenario": scenario}, upsert=True)
    swarm().incidents.insert_one({"_id": f"INC-{run_id}", "run_id": run_id, "status": status, "active": active,
                                  "closed_at": now() - timedelta(seconds=closed_age)})


def test_first_approval_stays_manual(clean):
    ap = Autoplay(gap_s=0, approve_s=3)
    _apr("APR-0001")
    assert ap.approve_pending() == []
    assert swarm().approvals.find_one({"_id": "APR-0001"})["status"] == "pending"


def test_later_approvals_auto_approve_after_delay_with_a_visible_line(clean):
    ap = Autoplay(gap_s=0, approve_s=3)
    _apr("APR-0001", status="approved")
    _apr("APR-0002", age=1)  # too fresh: stays on screen
    assert ap.approve_pending() == []
    swarm().approvals.update_one({"_id": "APR-0002"}, {"$set": {"ts": now() - timedelta(seconds=4)}})
    _apr("APR-0003", desc="Gardener upgrade UPG-0001", kind="upgrade")
    assert sorted(ap.approve_pending()) == ["APR-0002", "APR-0003"]
    d = swarm().approvals.find_one({"_id": "APR-0002"})
    assert d["status"] == "approved" and d["decided_by"] == "autoplay" and d["decided_at"]
    texts = [t["text"] for t in swarm().thoughts.find({"agent": "autoplay"})]
    assert any("APR-0002 auto-approved (same as last time)" in t for t in texts)
    assert any("APR-0003 auto-approved (demo autoplay)" in t for t in texts)
    assert ap.approve_pending() == []  # conditional update: never twice


def test_night1_advances_once_with_next_round_reset(clean, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "reset", lambda body: calls.append(("reset", body["mode"])) or {})
    monkeypatch.setattr(api, "start_scenario", lambda name: calls.append(("start", name)) or {"run_id": "RUN-0002"})
    ap = Autoplay(gap_s=4, approve_s=3)
    _estate()
    _night("bank-night-1", "RUN-0001", closed_age=1)
    ap.tick()
    assert calls == []  # gap not elapsed yet
    swarm().incidents.update_one({"run_id": "RUN-0001"}, {"$set": {"closed_at": now() - timedelta(seconds=5)}})
    ap.tick()
    ap.tick()
    assert calls == [("reset", "next_round"), ("start", "bank-night-2")]


def test_no_advance_while_unresolved_or_estate_red(clean, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "reset", lambda body: calls.append("reset") or {})
    monkeypatch.setattr(api, "start_scenario", lambda name: calls.append(name) or {})
    ap = Autoplay(gap_s=0, approve_s=3)
    _estate("degraded")
    _night("bank-night-1", "RUN-0001")
    ap.tick()
    _estate()
    swarm().incidents.update_one({"run_id": "RUN-0001"}, {"$set": {"status": "failed"}})
    ap.tick()
    assert calls == []


def test_night3_waits_for_the_proven_upgrade_and_skips_reset(clean, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "reset", lambda body: calls.append("reset") or {})
    monkeypatch.setattr(api, "start_scenario", lambda name: calls.append(name) or {})
    ap = Autoplay(gap_s=0, approve_s=3)
    _estate()
    _night("bank-night-2", "RUN-0002")
    ap.tick()
    assert calls == []  # no upgrade yet
    swarm().upgrade_requests.insert_one({"_id": "UPG-0001", "system": "mainframe", "fault_class": "batch_abort",
                                         "status": "deployed", "ts": now()})
    ap.tick()
    assert calls == []
    swarm().upgrade_requests.update_one({"_id": "UPG-0001"}, {"$set": {"status": "proven", "proven_at": now()}})
    ap.tick()
    ap.tick()
    assert calls == ["bank-night-3"]


def test_night3_is_the_end(clean, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "start_scenario", lambda name: calls.append(name) or {})
    _estate()
    swarm().stats.replace_one({"_id": "current"}, {"_id": "current", "run_id": "RUN-0003", "scenario": "bank-night-3"},
                              upsert=True)
    Autoplay(gap_s=0, approve_s=3).tick()
    assert calls == []
