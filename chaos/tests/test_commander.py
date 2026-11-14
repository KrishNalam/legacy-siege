"""Red Commander + Scouts (RED_COMMANDER=1): off contract, vetted combos only, and a full campaign
(plan -> Striker executes each step through runner.fire -> reflect) against MongoDB + the fake systems hook."""
from __future__ import annotations

from datetime import timedelta

import pytest

from chaos import runner
from chaos.redfleet import PERSONAS, Settings, commander
from chaos.redfleet.agent import Attacker
from common.db import now, swarm, world

from .test_redfleet import HIVE, INC, snap


def cfg(**kw) -> Settings:
    return Settings(**{"cooldown_s": 0, "use_llm": True, **kw})


@pytest.fixture
def fast(monkeypatch):
    for k in ("RED_COMMANDER_FIRST_S", "RED_COMMANDER_STEP_GAP_S", "RED_COMMANDER_OBSERVE_S", "RED_COMMANDER_COOLDOWN_S"):
        monkeypatch.setenv(k, "0")
    monkeypatch.setenv("RED_COMMANDER", "1")
    monkeypatch.setenv("RED_FLEET_AGENTS", "red-saboteur,red-drifter,red-liar")
    monkeypatch.setattr(commander.llm, "llm_available", lambda: False)
    from chaos.redfleet import agent as agent_mod
    monkeypatch.setattr(agent_mod.llm, "llm_available", lambda: False)


def _night_with_pending_mainframe_step():
    run_id = runner.start_run("free-play")
    world().runs.update_one({"_id": run_id}, {"$set": {"started_at": now() - timedelta(seconds=300)}})
    runner.announce_current(run_id, 1)
    swarm().incidents.insert_one({"_id": "INC-0001", "active": True, "status": "fixing", "system": "portal",
                                  "root_cause_system": "mainframe", "opened_at": now(), "run_id": run_id,
                                  "steps": [{"op": "rerun_batch", "system": "mainframe", "status": "planned"}]})
    for pid in ("red-saboteur", "red-drifter", "red-liar"):
        world().red_agents.insert_one({"_id": pid, "status": "hunting", "last_seen": now()})
    return run_id


def test_off_by_default_strikers_ignore_orders(systems, monkeypatch, fast):
    monkeypatch.delenv("RED_COMMANDER")
    assert not commander.enabled()
    run_id = _night_with_pending_mainframe_step()
    world().red_agents.update_one({"_id": "red-saboteur"}, {"$set": {"order": {
        "fault": "disk_fill", "target": "unix", "campaign": "CMP-x", "run_id": run_id, "combo": "c", "step": 0,
        "of": 2, "status": "assigned", "reason": "r"}}})
    out = Attacker(PERSONAS["red-saboteur"], cfg()).step()
    assert out["event"]["fault"] == "batch_abort" and "campaign" not in out["event"]["red"]   # its own playbook


def test_combos_are_vetted_and_feasible(monkeypatch):
    monkeypatch.setenv("CHAOS_AGENT_FREEZE", "1")
    monkeypatch.delenv("RED_FLEET_AGENTS", raising=False)
    s = snap(agents=HIVE, mid_send=[a for a in HIVE if a["phase"] == "step_sent"], incidents=[INC])
    got = {o["combo"]: o for o in commander.combos(s, cfg())}
    assert {"lie-then-freeze", "lie-then-kill", "false-log-then-kill", "break-then-drift"} <= set(got)
    lf = got["lie-then-freeze"]["steps"]
    assert lf[0] == {"persona": "red-liar", "fault": "lying_success", "target": "unix"}   # INC's pending step is on unix
    assert lf[1]["persona"] == "red-assassin" and lf[1]["fault"] == "agent_freeze" and lf[1]["victim"] == "worker-mainframe-1"
    monkeypatch.setenv("RED_FLEET_AGENTS", "red-liar,red-saboteur")   # Strikers that aren't running get no steps
    assert {o["combo"] for o in commander.combos(s, cfg())} == set()


def test_claude_pick_must_be_on_the_menu(systems, monkeypatch, fast):
    _night_with_pending_mainframe_step()
    monkeypatch.setattr(commander.llm, "llm_available", lambda: True)
    monkeypatch.setattr(commander.llm, "complete", lambda *a, **k: '{"combo": "nuke-it", "reason": "x"}')
    c = commander.Commander(cfg())
    c.step()
    camp = world().red_agents.find_one({"_id": commander.COMMANDER_ID})["campaign"]
    assert camp["planner"] == "policy" and camp["combo"] == "break-then-drift"
    assert swarm().thoughts.count_documents({"agent": commander.COMMANDER_ID, "text": {"$regex": "nuke-it.*not a feasible"}}) == 1
    world().red_agents.delete_one({"_id": commander.COMMANDER_ID})
    world().red_agents.update_many({}, {"$unset": {"order": ""}})
    monkeypatch.setattr(commander.llm, "complete", lambda *a, **k:
                        '{"combo": "false-log-then-kill", "reason": "logs lie because"}')
    monkeypatch.setenv("RED_FLEET_AGENTS", "red-liar,red-assassin")
    swarm().agents.insert_one({"_id": "worker-mainframe-1", "role": "worker", "status": "working", "phase": "step_sent",
                               "specialty": "mainframe", "incident": "INC-0001", "last_seen": now()})
    c.step()
    camp = world().red_agents.find_one({"_id": commander.COMMANDER_ID})["campaign"]
    assert camp["planner"] == "claude" and camp["combo"] == "false-log-then-kill" and camp["reason"] == "logs lie because"


def test_full_campaign_plan_execute_reflect(systems, fast):
    run_id = _night_with_pending_mainframe_step()
    c = commander.Commander(cfg())
    sab, dri = Attacker(PERSONAS["red-saboteur"], cfg()), Attacker(PERSONAS["red-drifter"], cfg())
    c.step()                                            # OBSERVE (scouts post) -> THINK -> ACT: step 1 assigned
    board = {d["_id"]: d for d in world().red_agents.find()}
    assert board["red-scout-hive"]["findings"]["pending_step_systems"] == ["mainframe"]
    assert "findings" in board["red-scout-systems"]
    camp = board[commander.COMMANDER_ID]["campaign"]
    assert camp["combo"] == "break-then-drift" and camp["planner"] == "policy"
    assert board["red-saboteur"]["order"]["status"] == "assigned" and board["red-drifter"]["order"]["status"] == "queued"
    assert "standing by" in dri.step()["hold"]          # step 2 waits its turn
    ev1 = sab.step()["event"]                           # Striker executes step 1 through runner.fire
    assert (ev1["fault"], ev1["target"], ev1["by"]) == ("batch_abort", "mainframe", "red-saboteur")
    assert ev1["red"]["campaign"] == {"id": camp["id"], "step": 1, "of": 2, "combo": "break-then-drift",
                                      "ordered_by": commander.COMMANDER_ID}
    c.step()                                            # advance: step 2 assigned
    ev2 = dri.step()["event"]
    assert ev2["fault"] == "schema_drift" and ev2["red"]["campaign"]["step"] == 2
    c.step()                                            # both landed -> observing
    c.step()                                            # REFLECT
    reg = world().red_agents.find_one({"_id": commander.COMMANDER_ID})
    assert "campaign" not in reg and reg["last_reflection"]["events"] == [ev1["_id"], ev2["_id"]]
    assert reg["last_reflection"]["outcome_summary"]["landed"] == "2/2" and reg["lessons"][-1]["run_id"] == run_id
    assert world().chaos_events.find_one({"_id": ev1["_id"]})["red"]["campaign_reflection"]["combo"] == "break-then-drift"
    texts = [t["text"] for t in swarm().thoughts.find({"agent": commander.COMMANDER_ID}).sort("ts", 1)]
    assert any("THINK: campaign break-then-drift" in t and "policy (no reasoning)" in t for t in texts)
    assert sum("ACT: step" in t for t in texts) == 2
    assert any("REFLECT (policy (no reasoning))" in t for t in texts) and any("LEARN:" in t for t in texts)


def test_unexecuted_step_expires_and_campaign_aborts(systems, fast, monkeypatch):
    monkeypatch.setenv("RED_COMMANDER_STEP_TIMEOUT_S", "0")
    _night_with_pending_mainframe_step()
    c = commander.Commander(cfg())
    c.step()
    import time
    time.sleep(0.05)
    c.step()                                            # nobody executed step 1 -> aborted
    c.step()                                            # reflect on the aborted campaign
    reg = world().red_agents.find_one({"_id": commander.COMMANDER_ID})
    assert reg["last_reflection"]["outcome_summary"] == {"landed": "0/2", "guard_caught": None, "recovery_s": None,
                                                         "nurse_handoffs": 0, "aborted": True}
    assert "order" not in world().red_agents.find_one({"_id": "red-saboteur"})
