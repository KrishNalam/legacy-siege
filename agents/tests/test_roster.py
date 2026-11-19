"""W22 Comms live roster: derived from agents + leases + handoffs; kill/takeover and scale-down stay visible."""
from datetime import timedelta

from common.db import col, now
from agents import roster
from agents.tests.conftest import reset_runtime


def test_roster_tracks_parent_death_takeover_and_retire(db):
    reset_runtime()  # earlier modules (test_gardener_help, test_lessons, ...) may leave scout-1 in agents
    t = now()
    col("roster").delete_many({})
    col("agents").insert_many([
        {"_id": "scout-1", "role": "scout", "status": "working", "target": "portal", "incident": "INC-0001",
         "phase": "fixing", "last_seen": t},
        {"_id": "worker-mainframe-1", "role": "worker", "specialty": "mainframe", "status": "dead",
         "died_at": t, "last_seen": t - timedelta(seconds=9)},
        {"_id": "nurse-1", "role": "nurse", "status": "working", "target": "mainframe", "incident": "INC-0001",
         "task": "INC-0001-t4", "phase": "takeover", "last_seen": t},
        {"_id": "worker-unix-2", "role": "worker", "specialty": "unix", "status": "idle", "spawned": True,
         "last_seen": t},
    ])
    col("incidents").insert_one({
        "_id": "INC-0001", "active": True, "status": "fixing", "system": "portal", "owner": "scout-1",
        "assigned_by": "queen-1", "opened_at": t,
        "steps": [{"step_id": "INC-0001-s1", "op": "quarantine_records", "status": "sent",
                   "system": "mainframe"}],
        "handoffs": [{"from": "worker-mainframe-1", "to": "nurse-1", "work": "INC-0001-t4", "ts": t,
                      "takeover_s": 4.2}]})
    col("tasks").insert_one({"_id": "INC-0001-t4", "incident": "INC-0001", "status": "claimed", "owner": "nurse-1",
                             "specialty": "mainframe", "step_ids": ["INC-0001-s1"]})
    col("spawn_requests").insert_one({"_id": "SPW-0001", "status": "spawned", "agent_id": "worker-unix-2",
                                      "requested_by": "scout-1"})
    r = roster.Roster()
    assert r.sweep() == 4
    docs = {d["_id"]: d for d in col("roster").find()}
    assert docs["scout-1"]["parent"] == "queen-1" and docs["scout-1"]["system"] == "portal"
    nurse = docs["nurse-1"]
    assert nurse["parent"] == "scout-1" and nurse["step"] == "INC-0001-s1 quarantine_records (sent)"
    assert nurse["took_over"]["from"] == "worker-mainframe-1" and "took over from worker-mainframe-1" in nurse["doing"]
    dead = docs["worker-mainframe-1"]
    assert dead["state"] == "dead" and dead["taken_over_by"]["to"] == "nurse-1" and "nurse-1" in dead["doing"]
    assert docs["worker-unix-2"]["spawned_by"] == "scout-1"
    assert r.sweep() == 0  # nothing changed: no writes
    col("agents").delete_one({"_id": "worker-unix-2"})  # scaled down (retire deletes the agent doc)
    r.sweep()
    assert col("roster").find_one({"_id": "worker-unix-2"})["state"] == "retired"


def test_retired_row_expires_across_full_refresh_sweep(db, monkeypatch):
    """RF1: the every-Nth full-refresh sweep must not forget retired agents (row would never expire)."""
    reset_runtime()
    t0 = now()
    col("roster").delete_many({})
    col("agents").insert_many([{"_id": "scout-1", "role": "scout", "status": "idle", "last_seen": t0},
                               {"_id": "worker-unix-9", "role": "worker", "status": "idle", "last_seen": t0}])
    clock = {"t": t0}
    monkeypatch.setattr(roster, "now", lambda: clock["t"])
    r = roster.Roster()
    r.sweep()
    col("agents").delete_one({"_id": "worker-unix-9"})  # scaled down
    r.sweep()
    assert col("roster").find_one({"_id": "worker-unix-9"})["state"] == "retired"
    while r.n % roster.FULL_REFRESH_EVERY != roster.FULL_REFRESH_EVERY - 1:
        r.sweep()
    r.sweep()  # the full-refresh sweep, inside the retired keep window
    assert r.n % roster.FULL_REFRESH_EVERY == 0
    clock["t"] = t0 + timedelta(seconds=roster.RETIRED_KEEP_S + 5)
    r.sweep()
    assert col("roster").find_one({"_id": "worker-unix-9"}) is None
    assert col("roster").find_one({"_id": "scout-1"})["state"] == "idle"
