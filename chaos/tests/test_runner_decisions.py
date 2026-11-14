"""Runner decisions (RUNNER_DECIDES=1): off contract, every decision logged to chaos_events, stage caps honoured,
and each choice type reachable (Planner request, memory poison, seeded replay; execute vs spawn; Claude pick mocked)."""
from __future__ import annotations

import time
from datetime import timedelta

import pytest

from chaos import decider, runner
from common.db import now, swarm, world


def _decisions(**q) -> list[dict]:
    return list(world().chaos_events.find({"kind": "decision", "by": "runner", **q}).sort("ts", 1))


def _night(scenario: str = "free-play", age_s: float = 300) -> str:
    run_id = runner.start_run(scenario)
    world().runs.update_one({"_id": run_id}, {"$set": {"started_at": now() - timedelta(seconds=age_s)}})
    runner.announce_current(run_id, 1)
    return run_id


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("RUNNER_DECIDES", "1")
    for k, v in {"RUNNER_GAP_S": "0", "RUNNER_FIRST_S": "0", "RUNNER_MAX_PER_NIGHT": "3"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RUNNER_LLM", raising=False)
    decider._said.clear()


def _red_fire(fault="disk_fill", target="unix", by="red-saboteur", **kw):
    return runner.fire(fault, target, kw.pop("params", None), manual=False, extra={"by": by, "red": {"reason": "r"}}, **kw)


def test_off_by_default_fire_is_unchanged(systems, monkeypatch):
    monkeypatch.delenv("RUNNER_DECIDES", raising=False)
    _night()
    for _ in range(5):
        assert _red_fire()["status"] == "ok"
    assert _decisions() == [] and world().red_agents.find_one({"_id": decider.BOARD_ID}) is None


def test_every_red_fire_is_admitted_and_logged(systems, on):
    _night()
    ev = _red_fire()
    assert ev["status"] == "ok" and ev["runner_decision"].startswith("DEC-")
    d = world().chaos_events.find_one({"_id": ev["runner_decision"]})
    assert d["decision"] == "execute" and d["event"] == ev["_id"] and d["origin"] == "striker"
    assert d["choice"] == {"fault": "disk_fill", "target": "unix", "by": "red-saboteur"} and d["reason"]
    assert d["kind"] == "decision" and "fault" not in d and "target" not in d   # fault readers skip decision docs
    assert world().chaos_events.count_documents({"fault": {"$exists": True}}) == 1
    assert [e["_id"] for e in runner.refresh_outcomes()] == [ev["_id"]]          # outcome readers skip it too
    feed = [t["text"] for t in swarm().thoughts.find({"agent": "runner"})]
    assert any(d["_id"] in t and "execute disk_fill on unix" in t for t in feed)
    # manual buttons and seeded steps are never admitted
    assert runner.fire("disk_fill", "unix")["status"] == "ok"
    assert len(_decisions()) == 1


def test_caps_budget_gap_quiet_incident_and_victims(systems, on, monkeypatch):
    run_id = _night()
    monkeypatch.setenv("RUNNER_MAX_PER_NIGHT", "2")
    assert _red_fire()["status"] == "ok" and _red_fire("session_leak", "portal")["status"] == "ok"
    denied = _red_fire("cron_stop", "unix")
    assert denied["status"] == "deny" and "budget spent (2/2)" in denied["result"]["error"]
    assert world().chaos_events.count_documents({"fault": "cron_stop"}) == 0          # nothing fired

    _night()   # new night: fresh budget; now the gap
    monkeypatch.setenv("RUNNER_GAP_S", "60")
    assert _red_fire()["status"] == "ok"
    held = _red_fire("session_leak", "portal")
    assert held["status"] == "hold" and "spacing strikes" in held["result"]["error"]

    _night()
    monkeypatch.setenv("RUNNER_GAP_S", "0")
    swarm().incidents.insert_one({"_id": "INC-0009", "active": True, "status": "fixing", "opened_at": now()})
    held = _red_fire()
    assert held["status"] == "hold" and "mid-repair" in held["result"]["error"]       # R1 finding 3
    swarm().agents.insert_one({"_id": "queen-1", "role": "queen", "status": "idle", "last_seen": now()})
    q = _red_fire("agent_kill", "swarm", by="red-assassin", params={"selector": {"agent": "queen-1"}, "wait_s": 0})
    assert q["status"] == "deny" and "queen" in q["result"]["error"]                   # R1 finding 1

    _night("bank-night-3")
    assert _red_fire()["status"] == "deny"
    verdicts = [d["decision"] for d in _decisions()]
    assert verdicts.count("execute") == 3 and verdicts.count("deny") == 3 and verdicts.count("hold") == 2
    assert run_id


def test_concurrent_strikers_cannot_both_take_the_last_slot(systems, on, monkeypatch):
    import threading
    _night()
    monkeypatch.setenv("RUNNER_MAX_PER_NIGHT", "1")
    out = []
    ts = [threading.Thread(target=lambda: out.append(_red_fire()["status"])) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out).count("ok") == 1


def test_seeded_steps_always_execute_and_are_logged(systems, on):
    run_id = runner.run_scenario("bank-night-1", block=True)
    seeded = _decisions(origin="seeded")
    assert [d["choice"]["fault"] for d in seeded] == ["lying_success", "batch_abort"]
    assert all(d["decision"] == "execute" and d["run_id"] == run_id for d in seeded)


def _wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if got:
            return got
        time.sleep(0.05)
    return pred()


def test_loop_picks_planner_request_and_executes_it(systems, on):
    _night()
    loop = decider.RunnerLoop()
    assert loop.tick() is None                      # nothing to decide: no log spam
    rid = decider.propose("session_leak", "portal", by="red-planner", why="portal is the weak spot")
    d = loop.tick()
    assert d["decision"] == "execute" and d["choice"]["request"] == rid
    pick = _decisions(decision="pick")[-1]
    assert pick["origin"] == "planner" and pick["candidates"][0]["request"] == rid
    ev = _wait(lambda: world().chaos_events.find_one({"fault": "session_leak", "status": "ok"}))
    assert ev["by"] == decider.RUNNER_ID and ev["runner_decision"] == d["_id"] and ev["red"]["planner"] == "planner"
    req = world().red_agents.find_one({"_id": "runner"})["requests"][0]
    assert req["status"] == "executed" and req["decision"] == d["_id"]
    assert loop.tick() is None                      # request consumed


def test_loop_poisons_learned_memory(systems, on, monkeypatch):
    monkeypatch.setenv("CHAOS_POISON_MEMORY", "1")
    _night()
    swarm().playbooks.insert_one({"_id": "PB-0001", "fault_class": "disk_fill", "system": "unix", "stale": False,
                                  "created_at": now(), "fix": [{"op": "purge_spool", "system": "unix"}]})
    d = decider.RunnerLoop().tick()
    assert d["decision"] == "execute" and d["origin"] == "poison" and d["route"] == "execute"
    _wait(lambda: swarm().playbooks.find_one({"_id": "RED-POISON:PB-0001"}))
    assert _wait(lambda: world().chaos_events.find_one({"fault": "poison_memory", "status": "ok"}))
    assert not [c for c in decider.candidates(decider.state(), decider.Caps.from_env()) if c["source"] == "poison"]


def test_loop_replays_seeded_attack_only_on_unprotected_nights(systems, on):
    _night("bank-night-1")
    assert decider.RunnerLoop().tick() is None      # protected: round-2 timing stays honest
    _night("bank-drift")
    d = decider.RunnerLoop().tick()
    assert d["origin"] == "seeded" and d["choice"]["fault"] == "schema_drift" and d["decision"] == "execute"
    _wait(lambda: world().chaos_events.find_one({"fault": "schema_drift", "by": decider.RUNNER_ID}))
    assert decider.RunnerLoop().tick() is None      # once per night


def test_loop_spawns_striker_for_agent_victims_and_respects_spawn_budget(systems, on, monkeypatch):
    _night()
    spawned = []
    monkeypatch.setattr(decider.RunnerLoop, "spawn", lambda self, p: (spawned.append(p),
                        world().red_agents.update_one({"_id": "runner"}, {"$inc": {"spawns": 1}})))
    decider.propose("agent_kill", "swarm", {"selector": {"role": "worker", "phase": "step_sent"}}, by="red-planner")
    d = decider.RunnerLoop().tick()
    assert d["decision"] == "spawn" and d["persona"] == "red-assassin" and spawned == ["red-assassin"]
    assert world().chaos_events.count_documents({"fault": "agent_kill"}) == 0   # the Striker fires, admitted later
    decider.propose("agent_kill", "swarm", {"selector": {"role": "worker", "phase": "step_sent"}, "wait_s": 0},
                    by="red-planner")
    d2 = decider.RunnerLoop().tick()
    assert d2["decision"] == "execute" and "spawn budget spent" in d2["reason"] and spawned == ["red-assassin"]


def test_held_request_stays_queued_and_runs_when_clear(systems, on):
    _night()
    swarm().incidents.insert_one({"_id": "INC-0001", "active": True, "status": "fixing", "opened_at": now()})
    decider.propose("disk_fill", "unix")
    loop = decider.RunnerLoop()
    assert loop.tick()["decision"] == "hold"
    assert world().red_agents.find_one({"_id": "runner"})["requests"][0]["status"] == "held"
    swarm().incidents.update_one({"_id": "INC-0001"}, {"$set": {"active": False, "status": "resolved"}})
    assert loop.tick()["decision"] == "execute"


def test_claude_pick_is_mocked_and_must_be_on_the_menu(systems, on, monkeypatch):
    from common import llm
    monkeypatch.setenv("RUNNER_LLM", "1")
    monkeypatch.setenv("CHAOS_POISON_MEMORY", "1")
    monkeypatch.setattr(llm, "llm_available", lambda: True)
    _night()
    swarm().playbooks.insert_one({"_id": "PB-0002", "fault_class": "disk_fill", "system": "unix", "stale": False,
                                  "created_at": now(), "fix": [{"op": "purge_spool", "system": "unix"}]})
    decider.propose("session_leak", "portal")
    calls = []
    monkeypatch.setattr(llm, "complete", lambda *a, **k: calls.append(a) or '{"i": 1, "reason": "memory first"}')
    cands = decider.candidates(decider.state(), decider.Caps.from_env())
    c, how, why = decider.pick(cands, decider.state())
    assert (c["source"], how, why) == ("poison", "claude", "memory first") and len(calls) == 1
    monkeypatch.setattr(llm, "complete", lambda *a, **k: '{"i": 7}')            # off the menu -> rule
    c, how, _ = decider.pick(cands, decider.state())
    assert (c["source"], how) == ("planner", "rule")
