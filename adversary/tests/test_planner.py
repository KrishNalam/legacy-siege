"""Red Planner (RED_PLANNER=1, needs RED_COMMANDER=1): off contract, both scout types spawned per campaign, the
brief reaches the Commander, Claude's focus is vetted (mocked), and the hand-off to the Runner via decider.propose."""
from __future__ import annotations

from adversary import decider, decisions
from adversary.redfleet import PERSONAS, commander, planner
from adversary.redfleet.agent import Attacker
from common.db import now, world

from .test_commander import _night_with_pending_mainframe_step, cfg, fast  # noqa: F401 (fixture)


def _mine():
    return [d["decision"] for d in decisions.decisions(by=planner.PLANNER_ID)]


def test_off_by_default_and_needs_the_commander(systems, monkeypatch, fast):
    monkeypatch.delenv("RED_PLANNER", raising=False)
    assert not planner.enabled()
    _night_with_pending_mainframe_step()
    commander.Commander(cfg()).step()
    camp = world().red_agents.find_one({"_id": commander.COMMANDER_ID})["campaign"]
    assert "planning_id" not in camp and _mine() == []
    assert world().red_agents.find_one({"_id": planner.SCOUT_SWARM}) is None
    monkeypatch.setenv("RED_PLANNER", "1")
    monkeypatch.delenv("RED_COMMANDER")
    assert not planner.enabled()


def test_planner_spawns_both_scouts_and_briefs_the_commander(systems, monkeypatch, fast):
    monkeypatch.setenv("RED_PLANNER", "1")
    monkeypatch.delenv("RUNNER_DECIDES", raising=False)
    _night_with_pending_mainframe_step()
    c = commander.Commander(cfg())
    c.step()
    board = {d["_id"]: d for d in world().red_agents.find()}
    for sid in (planner.SCOUT_SWARM, planner.SCOUT_LEGACY):
        assert board[sid]["spawned_by"] == planner.PLANNER_ID and board[sid]["kind"] == "scout"
    assert board[planner.SCOUT_SWARM]["findings"]["pending_step_systems"] == ["mainframe"]
    assert "weak" in board[planner.SCOUT_LEGACY]["findings"]
    brief = board[planner.PLANNER_ID]["brief"]
    camp = board[commander.COMMANDER_ID]["campaign"]
    assert brief["planner"] == "rule" and brief["focus"] == "compound" and camp["combo"] in brief["prefer"]
    assert (camp["planning_id"], camp["focus"]) == (brief["planning_id"], "compound")
    assert _mine() == ["spawn", "spawn", "campaign_brief", "handoff"]           # no Runner: Striker orders
    assert decisions.decisions(by=planner.PLANNER_ID)[-1]["route"] == "orders"
    assert board["red-saboteur"]["order"]["planning_id"] == brief["planning_id"]
    ev = Attacker(PERSONAS["red-saboteur"], cfg()).step()["event"]
    assert ev["red"]["campaign"]["planning_id"] == brief["planning_id"] and ev["red"]["campaign"]["focus"] == "compound"


def test_claude_focus_must_be_on_the_menu(systems, monkeypatch, fast):
    run_id = _night_with_pending_mainframe_step()
    from adversary.redfleet import core
    s = core.scout(commander.COMMANDER_ID)
    options = commander.combos(s, cfg())
    monkeypatch.setattr(planner.llm, "llm_available", lambda: True)
    monkeypatch.setattr(planner.llm, "complete", lambda *a, **k: '{"focus": "compound", "priority": "high", "reason": "stack"}')
    counts = {}
    b = planner.plan(s, options, counts=counts)
    assert (b["planner"], b["focus"], b["priority"], b["reason"]) == ("claude", "compound", "high", "stack")
    assert counts == {"llm_calls": 1}
    monkeypatch.setattr(planner.llm, "complete", lambda *a, **k: '{"focus": "memory", "priority": "high"}')
    b = planner.plan(s, options, counts=counts)   # memory isn't feasible here -> rules
    assert b["planner"] == "rule" and b["focus"] == "compound" and counts["fallbacks"] == 1
    briefs = decisions.decisions(run_id, by=planner.PLANNER_ID)
    assert [d["planner"] for d in briefs if d["decision"] == "campaign_brief"] == ["claude", "rule"]


def test_campaign_handed_to_the_runner(systems, monkeypatch, fast):
    monkeypatch.setenv("RED_PLANNER", "1")
    monkeypatch.setenv("RUNNER_DECIDES", "1")
    _night_with_pending_mainframe_step()
    c = commander.Commander(cfg())
    c.step()
    camp = world().red_agents.find_one({"_id": commander.COMMANDER_ID})["campaign"]
    assert camp["handed"] and len(camp["requests"]) == 2
    assert "order" not in world().red_agents.find_one({"_id": "red-saboteur"})   # the Runner owns it, not Strikers
    reqs = {r["id"]: r for r in world().red_agents.find_one({"_id": decider.BOARD_ID})["requests"]}
    first = reqs[camp["requests"][0]]
    assert first["by"] == planner.PLANNER_ID and first["status"] == "queued" and first["prefer"] == "execute"
    assert first["campaign"]["planning_id"] == camp["planning_id"] and first["campaign"]["step"] == 1
    assert _mine() == ["spawn", "spawn", "campaign_brief", "propose", "propose", "handoff"]
    c.step()
    assert world().red_agents.find_one({"_id": commander.COMMANDER_ID})["campaign"]["status"] == "active"
    for rid in camp["requests"]:   # the Runner decided: executed + its CHX carries red.request
        decider._set_request(rid, "executed", "DEC-x")
        world().chaos_events.insert_one({"_id": f"CHX-{rid}", "fault": reqs[rid]["fault"], "status": "ok",
                                         "ts": now(), "red": {"request": rid}})
    c.step()
    camp = world().red_agents.find_one({"_id": commander.COMMANDER_ID})["campaign"]
    assert camp["status"] == "observing" and [s["event"] for s in camp["steps"]] == [f"CHX-{r}" for r in camp["requests"]]
