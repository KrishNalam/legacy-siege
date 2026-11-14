"""SWARM_DYNAMIC on/off contract, and the demo moments with dynamic scaling on (real supervisor + processes):
off -> the fixed 12-bee roster (unchanged); on -> 7 standing bees, the Queen spawns a scout for the incident,
the scout spawns the specialist workers each task needs, the cascade is still traced portal→unix→mainframe,
the lie is still caught, a SIGKILLed spawned worker is still taken over by a warm nurse from its checkpoint,
round 2 still recalls (1 hop), and every spawned bee retires once the incident is closed."""
from __future__ import annotations

import os
import signal

import pytest

from swarm.tests.conftest import reset_runtime, run_until_resolved, start_swarm, wait_for


def test_switch_off_keeps_fixed_roster(monkeypatch):
    from swarm import scale, supervisor

    monkeypatch.delenv("SWARM_DYNAMIC", raising=False)
    monkeypatch.delenv("SWARM_ROSTER", raising=False)
    assert not scale.enabled() and supervisor.roster() == supervisor.ROSTER
    assert scale.ensure_scout("queen-1", "INC-X") is None  # no requests at all when off
    monkeypatch.setenv("SWARM_DYNAMIC", "1")
    assert scale.enabled() and supervisor.roster() == supervisor.DYNAMIC_ROSTER
    assert not any(r in ("scout", "worker") for r, _ in supervisor.DYNAMIC_ROSTER)


def test_supervisor_death_takes_its_agents_and_spares_along(db, fake):
    """S1 #2: a crashed/killed supervisor must not leave its agents running (run.py would start a second set)."""
    from common.db import col
    from swarm.tests.conftest import Swarm

    reset_runtime()
    s = Swarm({"SWARM_DYNAMIC": "1", "SWARM_ROSTER": "waggle:waggle-1,guard:guard-1"}, log_name="swarm_lifecycle")
    try:
        docs = wait_for(lambda: (lambda d: d if len(d) == 2 else None)(
            list(col("agents").find({"status": {"$ne": "dead"}}))), 60, 0.2, "2 agents")
        children = [int(x) for x in open(f"/proc/{s.p.pid}/task/{s.p.pid}/children").read().split()]
        assert {d["pid"] for d in docs} <= set(children) and len(children) >= 3  # + warm spare(s)
        os.kill(s.p.pid, signal.SIGKILL)
        s.p.wait(10)

        def gone():
            for pid in children:
                try:
                    os.kill(pid, 0)
                    with open(f"/proc/{pid}/stat") as f:
                        if f.read().split(") ")[1].startswith("Z"):
                            continue  # exited, waiting to be reaped by init
                    return False
                except ProcessLookupError:
                    continue
            return True
        wait_for(gone, 15, 0.2, "orphaned agents and warm spares to exit")
    finally:
        s.stop()
    reset_runtime()


@pytest.fixture(scope="module")
def hive(db, fake):
    reset_runtime()
    fake.post("/_admin/reset")
    s = start_swarm({"SWARM_DYNAMIC": "1", "STEP_HOLD_S": "1.5", "SWARM_RETIRE_AFTER_S": "1",
                     "SWARM_IDLE_S": "6"}, log_name="swarm_dynamic", agents=7)
    yield s
    s.stop()


def _spawned_live():
    from common.db import col
    return list(col("agents").find({"spawned": True, "status": {"$ne": "dead"}}))


def test_dynamic_round1_spawns_specialists_kill_takeover_and_retires(hive, fake):
    from common.db import col

    assert col("agents").count_documents({"role": {"$in": ["scout", "worker"]}}) == 0  # no standing pool
    col("stats").update_one({"_id": "current"}, {"$set": {"run_id": "RUN-D1", "round": 1}}, upsert=True)
    fake.post("/mainframe/_chaos/lying_success", json={"params": {"count": 1}})
    fake.post("/mainframe/_chaos/batch_abort", json={"params": {}})

    # kill the first spawned worker caught mid-step (what the judge's kill button does)
    victim = wait_for(lambda: col("agents").find_one({"role": "worker", "phase": "step_sent"}), 90, 0.05,
                      "a spawned worker in phase step_sent")
    assert victim.get("spawned") is True
    os.kill(victim["pid"], signal.SIGKILL)

    inc = run_until_resolved(1, timeout=150)
    assert inc["diagnosis_path"] == ["portal", "unix", "mainframe"]           # cascade traced
    s1 = inc["steps"][0]
    assert len(s1["attempts"]) == 2                                           # lie caught, new attempt
    assert col("thoughts").find_one({"kind": "reject", "text": {"$regex": "ACK ≠ APPLIED"}})
    hs = [h for h in inc["handoffs"] if h["from"] == victim["_id"]]           # warm nurse took over
    assert hs and hs[0]["to"].startswith("nurse-") and hs[0]["checkpoint_id"]
    assert set(fake.get("/_fake/receipts").json()["applied"].values()) == {1}  # exactly once

    reqs = list(col("spawn_requests").find({"status": "spawned"}))
    roles = sorted({(r["role"], r["specialty"]) for r in reqs}, key=str)
    assert ("scout", None) in roles
    assert {("worker", "web"), ("worker", "unix"), ("worker", "mainframe")} <= set(roles)
    assert inc["owner"] in {r["agent_id"] for r in reqs if r["role"] == "scout"}
    assert col("thoughts").find_one({"kind": "spawn", "agent": {"$regex": "^queen-"}})

    # scale back down: every spawned bee retires (doc deleted), the killed one stays dead, bench refilled
    wait_for(lambda: not _spawned_live(), 40, 0.2, "spawned bees to retire")
    assert col("thoughts").find_one({"kind": "retire"})
    assert col("agents").find_one({"_id": victim["_id"]})["status"] == "dead"
    wait_for(lambda: col("agents").find_one({"role": "nurse", "standby_for": victim["_id"],
                                             "status": {"$ne": "dead"}}), 25, 0.2, "replacement nurse")
    sc = wait_for(lambda: col("stats").find_one({"_id": "swarm_scale", "spawned_live": 0}), 10, 0.2, "scale stats")
    assert sc["dynamic"] is True and sc["spawned"] >= 4 and sc["peak"] >= 7 + 2 and sc["live"] <= sc["max"]


def test_dynamic_round2_recall_one_hop(hive, fake):
    from common.db import col
    from swarm import recall

    first = col("incidents").find_one({"status": "resolved"})
    pb = col("playbooks").find_one({"incident": first["_id"]})
    wait_for(lambda: any(h["_id"] == pb["_id"] for h in recall.search(pb["symptom_text"])[1]), 30, 0.5,
             "playbook to be indexed")
    fake.post("/_admin/reset")
    col("stats").update_one({"_id": "current"}, {"$set": {"run_id": "RUN-D2", "round": 2}}, upsert=True)
    fake.post("/mainframe/_chaos/batch_abort", json={"params": {}})
    inc = run_until_resolved(2, timeout=120)
    assert inc["recall"]["used"] is True and inc["diagnosis_path"] == ["mainframe"]
    assert inc["metrics"]["diagnosis_s"] < first["metrics"]["diagnosis_s"]
    wait_for(lambda: not _spawned_live(), 40, 0.2, "spawned bees to retire")
