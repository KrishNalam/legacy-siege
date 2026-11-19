"""Red fleet: off by default, menus + validation, guardrails, per-persona playbooks, Claude or fallback,
and full graph passes against MongoDB + the fake systems hook."""
from __future__ import annotations

import importlib
from datetime import timedelta

import pytest

from adversary import runner
from adversary.redfleet import PERSONAS, Settings, core, gate, validate
from adversary.redfleet import __main__ as fleet_main
from adversary.redfleet.agent import Attacker
from common import config
from common.db import now, swarm, world

@pytest.fixture(autouse=True)
def fleet_switch(monkeypatch):
    monkeypatch.setenv("RED_FLEET", "1")


P = PERSONAS


def cfg(**kw) -> Settings:
    return Settings(**{"cooldown_s": 0, "use_llm": True, **kw})


def agent(id, role, status="working", phase=None, specialty=None, incident=None):
    return {"id": id, "role": role, "status": status, "phase": phase, "specialty": specialty, "incident": incident}


HIVE = [agent("guard-1", "guard"), agent("gardener-1", "gardener"), agent("queen-1", "queen"),
        agent("queen-2", "queen", "standby"), agent("scout-1", "scout", incident="INC-0001"),
        agent("worker-mainframe-1", "worker", phase="step_sent", specialty="mainframe", incident="INC-0001"),
        agent("worker-unix-1", "worker", "idle"), agent("nurse-1", "nurse", "standby"),
        agent("waggle-1", "waggle"), agent("sentinel-1", "sentinel")]


def snap(**kw) -> dict:
    base = {"run_id": "RUN-0001", "scenario": "bank-night-1", "night_running": True,
            "night_started": now() - timedelta(seconds=300), "incidents": [], "agents": [], "mid_send": [],
            "systems": {}, "approvals_pending": 0, "upgrade_busy": 0, "learned": [], "red_attacks": [],
            "fleet_attacks": []}
    return {**base, **kw}


INC = {"id": "INC-0001", "status": "fixing", "system": "portal", "root_cause_system": "mainframe",
       "pending_steps": [{"op": "rerun_transfer", "system": "unix", "status": "planned"}]}


# ------------------------------------------------------------------ off by default, live DBs refused
def test_off_by_default(monkeypatch, capsys):
    monkeypatch.delenv("RED_FLEET", raising=False)
    assert not core.enabled()
    import run
    assert "adversary" not in [n for n, _ in importlib.reload(run).PROCS]
    monkeypatch.setattr("sys.argv", ["adversary.redfleet"])
    fleet_main.main()
    assert "off" in capsys.readouterr().out
    assert swarm().thoughts.count_documents({}) == 0 and world().chaos_events.count_documents({}) == 0


def test_run_py_starts_the_fleet_when_switched_on(monkeypatch):
    monkeypatch.setenv("RED_FLEET", "1")
    import run
    try:
        assert "adversary" in [n for n, _ in importlib.reload(run).PROCS]
    finally:
        monkeypatch.delenv("RED_FLEET")
        importlib.reload(run)


def test_refuses_live_dbs(monkeypatch):
    monkeypatch.delenv("RED_FLEET_ALLOW_LIVE", raising=False)
    monkeypatch.setattr(config, "WORLD_DB", "world")
    assert core.live_db_refusal()
    monkeypatch.setenv("RED_FLEET_ALLOW_LIVE", "1")
    assert core.live_db_refusal() is None
    monkeypatch.delenv("RED_FLEET_ALLOW_LIVE")
    monkeypatch.setattr(config, "WORLD_DB", "world_w14")
    monkeypatch.setattr(config, "SWARM_DB", "swarm_w14")
    assert core.live_db_refusal() is None


def test_personas_cover_the_library():
    assert set(P) == {"red-saboteur", "red-assassin", "red-liar", "red-hacker", "red-drifter", "red-memory"}
    from adversary import faults
    covered = {f for p in P.values() for f in p.faults}
    assert covered <= set(faults.FAULTS) and covered == set(faults.FAULTS)


# ------------------------------------------------------------------ validation: menu only
def test_validate_rejects_anything_off_the_menu():
    s, c = snap(), cfg()
    liar = P["red-liar"]
    assert "library" in validate(liar, {"fault": "rm_rf", "target": "mainframe"}, s, c)[1]
    assert "kit" in validate(liar, {"fault": "batch_abort", "target": "mainframe"}, s, c)[1]
    assert validate(liar, {"fault": "false_log", "target": "unix", "blame": "unix"}, s, c)[0] is None
    assert validate(liar, {"fault": "lying_success", "target": "console"}, s, c)[0] is None
    assert validate(liar, "lying_success", s, c)[0] is None
    ok, _ = validate(liar, {"fault": "false_log", "target": "mainframe", "blame": "portal", "params": {"x": 1},
                            "reason": "r"}, s, c)
    assert ok["params"] == {"blame": "portal"} and ok["reason"] == "r"   # params come from the menu only
    ok, _ = validate(liar, {"fault": "lying_success", "target": "unix"}, s, c)
    assert ok["params"] == {"count": 1}


def test_allowlist_narrows_menus():
    c = cfg(allowed=("lying_success",))
    assert {m["fault"] for m in P["red-liar"].menu(snap(), c)} == {"lying_success"}
    assert validate(P["red-liar"], {"fault": "slow_apply", "target": "unix"}, snap(), c)[0] is None


def test_assassin_menu_spares_the_gardener_and_the_last_queen():
    menu = P["red-assassin"].menu(snap(agents=HIVE), cfg())
    victims = {m["victim"] for m in menu}
    assert {"gardener-1", "queen-2", "worker-unix-1", "nurse-1", "waggle-1"}.isdisjoint(victims)
    assert {"guard-1", "queen-1", "scout-1", "worker-mainframe-1", "sentinel-1"} <= victims
    lone = [a for a in HIVE if a["id"] != "queen-2"]
    assert "queen-1" not in {m["victim"] for m in P["red-assassin"].menu(snap(agents=lone), cfg())}
    assert validate(P["red-assassin"], {"fault": "agent_kill", "target": "swarm", "victim": "gardener-1"},
                    snap(agents=HIVE), cfg())[0] is None


def test_kill_params_come_from_the_menu():
    s, c = snap(agents=HIVE), cfg(kill_wait_s=4)
    ok, _ = validate(P["red-assassin"], {"fault": "agent_kill", "target": "swarm", "victim": "worker-mainframe-1",
                                         "selector": {"role": "queen"}}, s, c)
    assert ok["params"] == {"selector": {"role": "worker", "phase": "step_sent", "specialty": "mainframe"},
                            "wait_s": 4}
    ok, _ = validate(P["red-assassin"], {"fault": "agent_kill", "target": "swarm", "victim": "guard-1"}, s, c)
    assert ok["params"]["selector"] == {"agent": "guard-1"}


def test_hacker_skips_patched_vulns():
    systems = {"portal": {"open_vulns": []}, "unix": {"open_vulns": ["unix-world-writable-conf"]},
               "mainframe": {"open_vulns": ["mf-default-operator"]}}
    menu = P["red-hacker"].menu(snap(systems=systems), cfg())
    assert {m["fault"] for m in menu} == {"config_exploit", "credential_exploit"}


# ------------------------------------------------------------------ guardrails (pacing + no permanent damage)
def test_gate():
    c, t = cfg(cooldown_s=20), now()
    assert gate(snap(), c, 0, t) is None
    assert gate(snap(approvals_pending=2), c, 0, t) is None          # Krish waived the approval hold
    assert "no night" in gate(snap(night_running=False), c, 0, t)
    assert "off limits" in gate(snap(scenario="bank-night-3"), cfg(skip_scenarios=("bank-night-3",)), 0, t)
    assert "Gardener" in gate(snap(upgrade_busy=1), c, 0, t)
    old, recent = t - timedelta(seconds=60), t - timedelta(seconds=5)
    assert "budget" in gate(snap(red_attacks=[{"fault": "a", "ts": old, "status": "ok"}] * 2), cfg(max_attacks=2), 0, t)
    assert "budget" in gate(snap(red_attacks=[{"fault": "a", "ts": old, "status": "ok"}] * 2), c, 0, t)  # default 2
    assert gate(snap(red_attacks=[{"fault": "a", "ts": old, "status": "ok"}] * 9), cfg(max_attacks=0), 0, t) is None
    assert "in flight" in gate(snap(red_attacks=[{"fault": "a", "ts": old, "status": "firing"}]), c, 0, t)
    assert "cooling" in gate(snap(red_attacks=[{"fault": "a", "ts": recent, "status": "ok"}]), c, 0, t)
    assert "unfold" in gate(snap(night_started=t - timedelta(seconds=10)), c, 25, t)
    # Memory Striker (latent): plants before a (skipped) night's first incident so the next recall sees it
    skip, fresh = cfg(skip_scenarios=("bank-night-2",)), t - timedelta(seconds=2)
    early = snap(scenario="bank-night-2", night_started=fresh, run_incidents=0, poisonable=[{"id": "PB-0001"}])
    assert gate(early, skip, 30, t, latent=True) is None
    assert "off limits" in gate(early, skip, 30, t)                                      # not for the others
    assert "off limits" in gate({**early, "run_incidents": 1}, skip, 30, t, latent=True)  # recall already happened
    assert "off limits" in gate({**early, "poisonable": []}, skip, 30, t, latent=True)    # nothing to poison


# ------------------------------------------------------------------ deterministic playbooks
def test_playbooks():
    c = cfg()
    pick = lambda pid, s: P[pid].policy_fn(s, P[pid].menu(s, c))  # noqa: E731
    s = snap(incidents=[INC])
    assert (pick("red-saboteur", s)["fault"], pick("red-saboteur", s)["target"]) == ("disk_fill", "unix")
    assert (pick("red-liar", s)["fault"], pick("red-liar", s)["target"]) == ("lying_success", "unix")
    s2 = snap(incidents=[INC], red_attacks=[{"fault": "lying_success"}])
    assert pick("red-liar", s2)["fault"] == "slow_apply"
    calm = {**INC, "pending_steps": []}
    lie = pick("red-liar", snap(incidents=[calm]))
    assert lie["fault"] == "false_log" and lie["target"] == "mainframe" and lie["blame"] != "mainframe"
    assert pick("red-assassin", snap(agents=HIVE))["victim"] == "worker-mainframe-1"
    assert pick("red-assassin", snap(agents=[a for a in HIVE if a["phase"] != "step_sent"]))["victim"] == "scout-1"
    assert pick("red-hacker", snap())["fault"] == "credential_exploit"
    assert pick("red-drifter", snap())["fault"] == "schema_drift"
    assert pick("red-drifter", snap(systems={"unix": {"faults_active": ["schema_drift"]}})) is None
    drifted = snap(systems={"unix": {"feed_field": "avail_bal"}})
    assert "avail_bal" not in {m.get("to") for m in P["red-drifter"].menu(drifted, c)}
    for pid in P:   # every playbook pick is itself a legal menu item
        got = pick(pid, snap(agents=HIVE, incidents=[INC]))
        assert got is None or validate(P[pid], got, snap(agents=HIVE, incidents=[INC]), c)[0]


# ------------------------------------------------------------------ Claude or the playbook
@pytest.mark.parametrize("reply", [None, "not json", '{"fault": "rm_rf", "target": "portal"}', "[1, 2]"])
def test_bad_llm_reply_falls_back(monkeypatch, reply):
    from adversary.redfleet import agent as agent_mod
    monkeypatch.setattr(agent_mod.llm, "llm_available", lambda: True)
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: reply)
    strike, who, rejected = Attacker(P["red-saboteur"], cfg()).plan(snap())
    assert who == "policy" and strike["fault"] == "batch_abort"
    assert (rejected is not None) == (reply is not None and "rm_rf" in reply)


def test_claude_choice_is_used_when_on_the_menu(monkeypatch):
    from adversary.redfleet import agent as agent_mod
    monkeypatch.setattr(agent_mod.llm, "llm_available", lambda: True)
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k:
                        'ok {"fault": "false_log", "target": "portal", "blame": "mainframe", "reason": "wrong way"}')
    strike, who, _ = Attacker(P["red-liar"], cfg()).plan(snap())
    assert who == "claude" and strike["params"] == {"blame": "mainframe"} and strike["reason"] == "wrong way"


# ------------------------------------------------------------------ whole graph against MongoDB
def _open_night(scenario="free-play"):
    run_id = runner.start_run(scenario)
    world().runs.update_one({"_id": run_id}, {"$set": {"started_at": now() - timedelta(seconds=300)}})
    runner.announce_current(run_id, 1)
    return run_id


def _no_llm(monkeypatch):
    monkeypatch.setenv("RED_FLEET", "1")
    from adversary.redfleet import agent as agent_mod
    monkeypatch.setattr(agent_mod.llm, "llm_available", lambda: False)


def test_attackers_strike_through_the_runner(systems, monkeypatch):
    _no_llm(monkeypatch)
    run_id = _open_night()
    out = Attacker(P["red-liar"], cfg()).step()
    ev = world().chaos_events.find_one({"_id": out["event"]["_id"]})
    assert ev["by"] == "red-liar" and ev["run_id"] == run_id and ev["manual"] is False
    assert (ev["fault"], ev["target"], ev["params"]) == ("false_log", "mainframe", {"blame": "unix"})
    assert ev["red"]["persona"] == "Liar" and systems.calls[0] == ("/mainframe/_chaos/false_log",
                                                                   {"params": {"blame": "unix"}})
    reg = world().red_agents.find_one({"_id": "red-liar"})
    assert reg["attacks"] == 1 and reg["last_attack"]["fault"] == "false_log" and reg["status"] == "hunting"
    texts = [t["text"] for t in swarm().thoughts.find({"agent": "red-liar", "role": "red"})]
    assert any(t.startswith("Liar (policy (no reasoning)): false_log on mainframe blaming unix") for t in texts)
    assert any(t.startswith("Liar: hit landed") for t in texts)


def test_cap_and_cooldown_hold_per_agent(systems, monkeypatch):
    _no_llm(monkeypatch)
    _open_night()
    sab = Attacker(P["red-saboteur"], cfg(max_attacks=2))
    sab.step()
    sab.step()
    assert "budget" in sab.step()["hold"]
    assert world().chaos_events.count_documents({"by": "red-saboteur"}) == 2
    hacker = Attacker(P["red-hacker"], cfg(cooldown_s=600))   # siblings keep their own pacing
    hacker.step()
    assert "cooling" in hacker.step()["hold"]
    assert world().red_agents.find_one({"_id": "red-hacker"})["hold"] == "cooling down"


def test_no_night_no_attack(systems):
    assert Attacker(P["red-saboteur"], cfg()).step()["hold"] == "no night running"
    assert systems.calls == []


def test_assassin_arms_a_kill_for_the_next_send_and_victims_filter():
    calm = [a for a in HIVE if a["phase"] != "step_sent"]
    s, c = snap(agents=calm, incidents=[INC]), cfg(kill_wait_s=4)
    pick = P["red-assassin"].policy_fn(s, P["red-assassin"].menu(s, c))
    assert pick["victim"] == "next-worker-mid-send"
    ok, _ = validate(P["red-assassin"], pick, s, c)
    assert ok["params"] == {"selector": {"role": "worker", "phase": "step_sent"}, "wait_s": 25.0}
    stage = cfg(victims=("worker:step_sent",))   # stage mode: only the Q2 beat, never a queen or the guard
    assert {m["victim"] for m in P["red-assassin"].menu(snap(agents=HIVE), stage)} == {"worker-mainframe-1"}
    assert {m["victim"] for m in P["red-assassin"].menu(s, stage)} == {"next-worker-mid-send"}
    assert P["red-assassin"].menu(snap(agents=calm), stage) == []


def test_reasoning_reflects_real_readbacks_and_learns(systems, monkeypatch):
    from adversary.redfleet import agent as mod
    prompts = []
    monkeypatch.setattr(mod.llm, 'llm_available', lambda: True)
    def answer(system, prompt, **kw):
        prompts.append(prompt)
        if 'Reflect on' in system:
            return '{"reflection":"No resolved incident is visible.","lesson":"Try a delayed ACK during a pending step."}'
        return '{"fault":"false_log","target":"mainframe","blame":"unix","reason":"Plant a misleading note because no worker is mid-send yet."}'
    monkeypatch.setattr(mod.llm, 'complete', answer)
    _open_night()
    a = Attacker(P['red-liar'], cfg())
    a.step()
    reg = world().red_agents.find_one({'_id': 'red-liar'})
    assert reg['last_reflection']['planner'] == 'claude'
    assert reg['last_reflection']['outcome']['incidents'] == []
    assert reg['llm_calls'] == 2 and reg['fallback_count'] == 0
    a.step()
    assert 'Try a delayed ACK' in prompts[2]
    assert 'shared_red_board' in prompts[2]
    assert swarm().thoughts.count_documents({'text': {'$regex': 'REFLECT.*LEARN'}}) == 2


def test_outcome_reads_existing_victim_incident_and_guard_ledger(systems):
    from adversary.redfleet.learning import outcome
    event = {'_id': 'CHX-test', 'fault': 'agent_freeze', 'target': 'swarm', 'status': 'ok',
             'ts': now(), 'result': {'incident': 'INC-existing'}}
    swarm().incidents.insert_one({'_id':'INC-existing', 'opened_at':now()-timedelta(seconds=20),
        'closed_at':now()+timedelta(seconds=4), 'status':'resolved', 'steps':[],
        'handoffs':[{'from':'worker-1','to':'nurse-1'}]})
    swarm().ledger.insert_one({'incident':'INC-existing','status':'failed',
        'action_id':'a1','updated_at':now(),'evidence':'ACK ≠ APPLIED: record still present'})
    observed = outcome(event)
    assert observed['guard_caught'] is True and 3 <= observed['recovery_s'] <= 5
    assert observed['incidents'][0]['handoffs'][0]['to'] == 'nurse-1'
    world().chaos_events.insert_one(event)
    runner.refresh_outcomes()
    assert world().chaos_events.find_one({'_id':event['_id']})['outcome']['guard_caught'] is True


def test_outcome_counts_a_rejected_recalled_repair_as_a_guard_catch(systems):
    from adversary.redfleet.learning import outcome
    event = {'_id': 'CHX-poison', 'fault': 'poison_memory', 'target': 'swarm', 'status': 'ok', 'ts': now()}
    swarm().incidents.insert_one({'_id': 'INC-0002', 'opened_at': now(), 'status': 'fixing', 'steps': [],
                                  'recall': {'used': True, 'rejected': 'RED-POISON:PB-0001'}})
    observed = outcome(event)
    assert observed['guard_caught'] is True and observed['incidents'][0]['memory_rejected'] == 'RED-POISON:PB-0001'
    # the plant is recalled a night later (RUN-0002): the outcome follows the planted playbook, not the run
    later = {**event, '_id': 'CHX-poison-2', 'run_id': 'RUN-0001', 'result': {'playbook': 'RED-POISON:PB-0009'}}
    swarm().incidents.insert_one({'_id': 'INC-0003', 'run_id': 'RUN-0002', 'opened_at': now(), 'status': 'resolved',
                                  'closed_at': now(), 'steps': [],
                                  'recall': {'used': True, 'memory_rejected': ['RED-POISON:PB-0009'],
                                             'rejected': 'RED-POISON:PB-0009'}})
    observed = outcome(later)
    assert observed['guard_caught'] is True and [r['incident'] for r in observed['incidents']] == ['INC-0003']
