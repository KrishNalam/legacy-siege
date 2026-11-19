"""Scorecard aggregation (world.chaos_events x swarm.incidents) and the CLI."""
from __future__ import annotations

from adversary import cli, runner, score
from common.db import now, swarm, world


def _incident(_id, run_id, status="resolved", attempts_per_step=(1,), handoffs=0, **metrics):
    return {"_id": _id, "run_id": run_id, "status": status, "system": "portal", "opened_at": now(),
            "steps": [{"step_id": f"{_id}-s{i}", "status": "verified", "evidence": "x", "op": "op", "system": "m",
                       "attempts": [f"{_id}-s{i}-a{k}" for k in range(n)]} for i, n in enumerate(attempts_per_step)],
            "handoffs": [{"from": "w", "to": "nurse-1"}] * handoffs,
            "recall": {"used": metrics.pop("recall_used", False)}, "metrics": metrics}


def test_scorecard_joins_red_and_blue(systems):
    r1 = runner.run_scenario("bank-night-1", block=True)
    runner.fire("agent_kill", params={"wait_s": 0})          # fails: no agents -> counted fired, not landed
    r2 = runner.run_scenario("bank-night-2", block=True)
    r3 = runner.run_scenario("bank-night-3", block=True)
    swarm().incidents.insert_many([
        _incident("INC-0001", r1, attempts_per_step=(1, 2, 1, 1), handoffs=1,
                  ttr_s=120.0, diagnosis_s=40.0, hops=3, attempts=5, takeover_s=12.0),
        _incident("INC-0002", r2, attempts_per_step=(1, 1, 1, 1), ttr_s=60.0, diagnosis_s=4.0, hops=0,
                  attempts=4, recall_used=True),
    ])
    rows = {r["run_id"]: r for r in score.scorecard()}
    a, b, c = rows[r1], rows[r2], rows[r3]
    assert (a["faults_fired"], a["faults_landed"], a["manual_faults"]) == (3, 2, 1)
    assert a["faults"][:2] == ["lying_success@mainframe", "batch_abort@mainframe"]
    assert (a["incidents_opened"], a["resolved"], a["ttr_s"], a["diagnosis_s"], a["hops"]) == (1, 1, 120.0, 40.0, 3)
    assert (a["attempts"], a["lies_caught"], a["takeovers"], a["takeover_s"]) == (5, 1, 1, 12.0)
    assert (b["round"], b["hops"], b["lies_caught"], b["recall_used"]) == (2, 0, 0, 1)
    assert (c["faults_fired"], c["incidents_opened"], c["ttr_s"]) == (1, 0, None)  # upgrade held: no incident
    assert [r["round"] for r in score.scorecard()] == [1, 2, 3]
    assert score.scorecard(run_id=r2)[0]["run_id"] == r2


def test_cli_commands(systems, capsys):
    assert cli.main(["list"]) == 0
    assert cli.main(["run", "bank-night-1"]) == 0
    assert cli.main(["fire", "lying_success", "unix", "--params", '{"count": 2}']) == 0
    assert systems.calls[-1] == ("/unix/_chaos/lying_success", {"params": {"count": 2}})
    assert cli.main(["kill", "worker", "--phase", "step_sent", "--wait", "0"]) == 0
    assert cli.main(["clear"]) == 0 and systems.calls[-1][0] == "/_chaos/clear"
    assert cli.main(["events", "-n", "5"]) == 0
    assert cli.main(["score"]) == 0
    assert cli.main(["fire", "batch_abort", "portal"]) == 2   # invalid target -> clean error
    out = " ".join(capsys.readouterr().out.split())  # rich wraps long lines at the terminal width
    assert "batch_abort" in out and "Scorecard" in out and "kill skipped: no worker mid-step" in out
    assert world().chaos_events.count_documents({}) == 4
    assert swarm().stats.find_one({"_id": "current"})["round"] == 1
