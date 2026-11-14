"""Fault library, scenarios and the runner against a real fake hook server (isolated DBs, PACE=0)."""
from __future__ import annotations

import threading

import httpx
import pytest

from chaos import faults, runner, scenarios
from common import config
from common.db import world


def test_library_has_every_contract_fault():
    expected = {"batch_abort", "disk_fill", "cron_stop", "session_leak", "lying_success", "slow_apply",
                "sqli_exploit", "config_exploit", "agent_kill", "agent_freeze", "lie_to_agent", "schema_drift",  # schema_drift: additive (§8)
                "false_log", "credential_exploit",  # Red fleet: additive (§8)
                "poison_memory",  # W18 Memory Striker, CHAOS_POISON_MEMORY (default off)
                "workflow_drift"}  # W26 workflow drift: additive, switch WORKFLOW_DRIFT
    assert set(faults.FAULTS) == expected
    for f in faults.FAULTS.values():
        assert f.description and f.tests and f.default_target in f.targets
    assert faults.get("lying_success").default_params == {"count": 1}


def test_seed_is_idempotent_and_matches_contract():
    scenarios.seed()
    first = list(world().chaos_scenarios.find().sort("_id", 1))
    scenarios.seed()
    assert list(world().chaos_scenarios.find().sort("_id", 1)) == first
    by_id = {s["_id"]: s for s in first}
    assert set(by_id) == {"bank-night-1", "bank-night-2", "bank-night-3", "bank-drift", "free-play"}
    assert [(s["t"], s["fault"], s["target"], s["params"]) for s in by_id["bank-night-1"]["steps"]] == [
        (0, "lying_success", "mainframe", {"count": 1}), (1, "batch_abort", "mainframe", {})]
    for n in ("bank-night-2", "bank-night-3"):
        assert [(s["fault"], s["target"]) for s in by_id[n]["steps"]] == [("batch_abort", "mainframe")]
    assert by_id["free-play"]["steps"] == []
    assert all(isinstance(s["seed"], int) for s in first)


def test_fire_system_fault_calls_hook_and_logs_event(systems):
    ev = runner.fire("lying_success", "mainframe", {"count": 2})
    assert systems.calls == [("/mainframe/_chaos/lying_success", {"params": {"count": 2}})]
    assert ev["status"] == "ok" and ev["result"]["effect"] == "lying_success active on mainframe"
    assert ev["manual"] is True and ev["run_id"] is None and ev["_id"].startswith("CHX-")
    assert world().chaos_events.count_documents({}) == 1


def test_default_target_and_params(systems):
    ev = runner.fire("disk_fill")
    assert ev["target"] == "unix" and ev["params"] == {"pct": 97}
    assert systems.calls[0][0] == "/unix/_chaos/disk_fill"


def test_invalid_target_rejected_without_event(systems):
    with pytest.raises(faults.FaultError):
        runner.fire("batch_abort", "portal")
    with pytest.raises(faults.FaultError):
        runner.fire("no_such_fault", "portal")
    assert world().chaos_events.count_documents({}) == 0 and systems.calls == []


def test_hook_down_still_records_failed_event(monkeypatch):
    monkeypatch.setattr(config, "SYSTEMS_URL", "http://127.0.0.1:18799")  # nothing listens here
    ev = runner.fire("batch_abort", "mainframe")
    assert ev["status"] == "failed" and "Connect" in ev["result"]["error"]


def test_hook_http_error_via_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    faults.set_http_client(httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        ev = runner.fire("cron_stop", "unix")
    finally:
        faults.set_http_client(None)
    assert ev["status"] == "failed" and ev["result"]["http_status"] == 500


def test_start_run_auto_increments_round_and_closes_previous():
    r1 = runner.start_run("bank-night-1")
    r2 = runner.start_run("bank-night-2")
    r3 = runner.start_run("free-play", round=7)
    r4 = runner.start_run("bank-night-3")
    runs = {r["_id"]: r for r in world().runs.find()}
    assert [runs[r]["round"] for r in (r1, r2, r3, r4)] == [1, 2, 7, 8]
    assert runs[r1]["ended_at"] and runs[r2]["ended_at"] and runs[r3]["ended_at"]
    assert runs[r4]["ended_at"] is None and runner.current_run()["_id"] == r4
    assert runs[r1]["seed"] == 1001


def test_run_scenario_background_fires_steps_in_order(systems):
    run_id = runner.run_scenario("bank-night-1")  # returns immediately
    assert runner.wait(run_id, timeout=10)
    evs = list(world().chaos_events.find({"run_id": run_id}).sort("_id", 1))
    assert [(e["fault"], e["target"], e["params"]) for e in evs] == [
        ("lying_success", "mainframe", {"count": 1}), ("batch_abort", "mainframe", {})]
    assert all(e["scenario"] == "bank-night-1" and e["round"] == 1 and e["seed"] == 1001
               and e["manual"] is False and e["status"] == "ok" for e in evs)
    assert [c[0] for c in systems.calls] == ["/mainframe/_chaos/lying_success", "/mainframe/_chaos/batch_abort"]
    run = world().runs.find_one({"_id": run_id})
    assert run["status"] == "played" and run["steps_fired"] == 2


def test_same_seed_same_timeline(systems):
    def timeline(run_id):
        return [(e["fault"], e["target"], e["params"], e["seed"])
                for e in world().chaos_events.find({"run_id": run_id}).sort("_id", 1)]

    a = runner.run_scenario("bank-night-1", block=True)
    b = runner.run_scenario("bank-night-1", block=True)
    assert timeline(a) == timeline(b) and len(timeline(a)) == 2


def test_manual_fire_attaches_to_current_run(systems):
    run_id = runner.run_scenario("free-play", block=True)
    ev = runner.fire("session_leak", "portal")
    assert ev["run_id"] == run_id and ev["scenario"] == "free-play" and ev["manual"] is True


def test_cancel_stops_pending_steps(systems, monkeypatch):
    monkeypatch.setattr(config, "PACE", 1.0)  # t=1 step now really waits 1 s
    run_id = runner.run_scenario("bank-night-1")
    runner.cancel(run_id)
    assert runner.wait(run_id, timeout=5)
    run = world().runs.find_one({"_id": run_id})
    assert run["status"] == "cancelled" and run["steps_fired"] <= 1


def test_concurrent_start_run_is_thread_safe():
    ids: list[str] = []
    threads = [threading.Thread(target=lambda: ids.append(runner.start_run("free-play"))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 8
    assert sorted(r["round"] for r in world().runs.find()) == list(range(1, 9))
    assert world().runs.count_documents({"ended_at": None}) == 1


def test_announce_current_mirrors_into_swarm_stats(systems):
    from common.db import swarm
    run_id = runner.run_scenario("bank-night-2", block=True, announce=True)
    cur = swarm().stats.find_one({"_id": "current"})
    assert cur["run_id"] == run_id and cur["round"] == 1


def test_schema_drift_fault_and_flagged_night3(monkeypatch):
    import importlib
    f = faults.get("schema_drift")
    assert f.targets == ("unix",) and faults.resolve_target(f, None) == "unix"
    assert [s["fault"] for s in scenarios.get("bank-drift")["steps"]] == ["schema_drift"]
    assert [s["fault"] for s in scenarios.get("bank-night-3")["steps"]] == ["batch_abort"]   # flag off: unchanged
    monkeypatch.setenv("SCHEMA_DRIFT_DEMO", "1")
    importlib.reload(scenarios)
    try:
        assert [s["fault"] for s in scenarios.get("bank-night-3")["steps"]] == ["batch_abort", "schema_drift"]
    finally:
        monkeypatch.delenv("SCHEMA_DRIFT_DEMO")
        importlib.reload(scenarios)
    assert [s["fault"] for s in scenarios.get("bank-night-3")["steps"]] == ["batch_abort"]  # stored copy follows code


def test_every_event_says_who_what_why(systems):
    run_id = runner.run_scenario("bank-night-1", block=True)
    seeded = list(world().chaos_events.find({"run_id": run_id}).sort("ts", 1))
    assert {(e["source"], e["who"]) for e in seeded} == {("scenario", "scenario:bank-night-1")}
    assert all(e["why"] for e in seeded)
    man = runner.fire("disk_fill")
    assert (man["source"], man["who"], man["why"]) == ("manual", "beekeeper", faults.get("disk_fill").tests)
    red = runner.fire("cron_stop", manual=False, extra={"by": "red-saboteur", "red": {"reason": "mid-repair"},
                                                        "who": "spoof", "source": "spoof"})
    assert (red["source"], red["who"], red["why"], red["by"]) == ("red-fleet", "red-saboteur", "mid-repair", "red-saboteur")


def test_poison_is_opt_in_wrong_system_and_reset_restores_real_memory(monkeypatch):
    from common.db import swarm
    monkeypatch.delenv("CHAOS_POISON_MEMORY", raising=False)
    with pytest.raises(faults.FaultError):
        runner.fire("poison_memory")
    monkeypatch.setenv("CHAOS_POISON_MEMORY", "1")
    batch = {"_id": "PB-batch", "fault_class": "batch_abort", "system": "mainframe", "stale": False,
             "fix": [{"system": "mainframe", "op": "quarantine_record", "params": {"record_id": "TX123456"}}]}
    swarm().playbooks.insert_one(batch)
    ev = runner.fire("poison_memory", manual=False, params={"shape": "wrong_record"})
    assert ev["status"] == "ok" and ev["result"]["shape"] == "wrong_record"     # W27: the second shape
    assert ev["result"]["wrong_fix"] == [{"system": "mainframe", "op": "quarantine_record",
                                          "params": {"record_id": "TX000000"}}]
    assert swarm().playbooks.find_one({"_id": "PB-batch"})["stale"]             # hidden behind the plant
    disk = {"_id": "PB-disk", "fault_class": "disk_fill", "system": "unix", "stale": False, "created_at": 1,
            "fix": [{"system": "unix", "op": "purge_spool", "params": {"path": "/var/spool/xfer"}}]}
    swarm().playbooks.insert_one(disk)
    ev = runner.fire("poison_memory", manual=False)
    assert ev["status"] == "ok" and ev["result"]["shape"] == "wrong_system" and ev["result"]["wrong_fix"] == [{"system": "mainframe", "op": "purge_spool"}]
    planted = swarm().playbooks.find_one({"_id": "RED-POISON:PB-disk"})
    assert planted["red_planted"] and planted["fix"][0]["system"] == "mainframe"
    assert planted["fix"][0]["params"] == {"path": "/var/spool/xfer"} and not planted["stale"]
    hidden = swarm().playbooks.find_one({"_id": "PB-disk"})
    assert hidden["stale"] and hidden["fix"] == disk["fix"]           # genuine repair hidden, never edited
    runner.cancel()                                                  # any reset
    assert swarm().playbooks.find_one({"_id": "PB-disk"}) == disk
    assert swarm().playbooks.find_one({"_id": "PB-batch"}) == batch
    assert not swarm().playbooks.count_documents({"red_planted": True})


def test_agent_lie_uses_vetted_log_hook_only_when_enabled(systems, monkeypatch):
    monkeypatch.delenv("CHAOS_AGENT_LIE", raising=False)
    with pytest.raises(faults.FaultError):
        runner.fire("lie_to_agent")
    monkeypatch.setenv("CHAOS_AGENT_LIE", "1")
    assert runner.fire("lie_to_agent", "unix")["status"] == "ok"
    assert systems.calls == [("/unix/_chaos/false_log", {"params": {"prompt_injection": True}})]


@pytest.mark.parametrize("fault", ["false_log", "credential_exploit"])
def test_red_fleet_system_attacks_require_master_switch(fault, systems, monkeypatch):
    monkeypatch.delenv("RED_FLEET", raising=False)
    with pytest.raises(faults.FaultError, match="switched off"):
        runner.fire(fault)
    assert not systems.calls
    monkeypatch.setenv("RED_FLEET", "1")
    assert runner.fire(fault)["status"] == "ok"
    assert len(systems.calls) == 1
