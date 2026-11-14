"""Chaos Monkey command line.

    python -m chaos.cli list                                   # faults + scenarios
    python -m chaos.cli run bank-night-1 [--round 2]           # play a scenario (waits for its steps)
    python -m chaos.cli fire batch_abort mainframe [--params '{"count":2}']
    python -m chaos.cli kill worker [--phase step_sent] [--wait 30]
    python -m chaos.cli kill --agent worker-unix-1
    python -m chaos.cli clear                                  # POST /_chaos/clear
    python -m chaos.cli events [-n 20]                         # recent chaos_events
    python -m chaos.cli score                                  # scorecard per run
"""
from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.table import Table

from common.db import world

from . import faults, runner, scenarios, score

console = Console()


def _fmt_result(ev: dict) -> str:
    res = ev.get("result") or {}
    if ev.get("status") == "ok":
        return f"[green]hit[/] {res.get('effect') or ''}".rstrip()
    return f"[red]failed[/] {res.get('error') or res.get('detail') or res}"


def _print_event(ev: dict) -> None:
    if ev.get("kind") == "decision":   # a Red Planner / Runner decision, not an attack
        console.print(f"[bold magenta]◆ {ev['_id']}[/] {ev.get('by')} {ev.get('decision')} "
                      f"[dim]{ev.get('run_id') or ''}[/]  {ev.get('reason') or ''}")
        return
    who = "manual" if ev.get("manual") else (ev.get("scenario") or "")
    console.print(f"[bold red]⚡ {ev['_id']}[/] {ev['fault']} → [bold]{ev['target']}[/] "
                  f"[dim]{json.dumps(ev['params'])} {ev.get('run_id') or ''} {who}[/]  {_fmt_result(ev)}")


def cmd_list(_a) -> None:
    t = Table(title="Fault library", header_style="bold red")
    for c in ("fault", "kind", "targets", "defaults", "effect", "what it tests"):
        t.add_column(c, overflow="fold")
    for f in faults.FAULTS.values():
        t.add_row(f.name, f.kind, ", ".join(f.targets), json.dumps(f.default_params) if f.default_params else "",
                  f.description, f.tests)
    console.print(t)
    s = Table(title="Scenarios", header_style="bold red")
    for c in ("scenario", "seed", "title", "steps"):
        s.add_column(c, overflow="fold")
    for scn in scenarios.list_all():
        steps = "; ".join(f"t={x['t']} {x['fault']}@{x['target']} {json.dumps(x.get('params') or {})}"
                          for x in scn["steps"]) or "(manual buttons)"
        s.add_row(scn["_id"], str(scn.get("seed")), scn.get("title", ""), steps)
    console.print(s)


def cmd_run(a) -> None:
    run_id = runner.run_scenario(a.scenario, round=a.round, block=True, announce=not a.no_announce,
                                 on_event=_print_event)
    run = world()["runs"].find_one({"_id": run_id})
    console.print(f"[bold]{run_id}[/] {a.scenario} round {run['round']} seed {run.get('seed')}: "
                  f"{run.get('steps_fired', 0)}/{run.get('steps_total', 0)} steps fired ({run.get('status')})")


def cmd_fire(a) -> None:
    params = json.loads(a.params) if a.params else None
    _print_event(runner.fire(a.fault, a.target, params, manual=True))


def cmd_kill(a) -> None:
    if a.agent:
        selector = {"agent": a.agent}
    else:
        selector = {"role": a.role}
        if a.phase:
            selector["phase"] = a.phase
    with console.status(f"waiting up to {a.wait:g}s for a victim matching {json.dumps(selector)}…"):
        ev = runner.fire("agent_kill", "swarm", {"selector": selector, "wait_s": a.wait}, manual=True)
    _print_event(ev)


def cmd_clear(_a) -> None:
    res = faults.clear_all()
    console.print("[green]cleared[/]" if res.get("ok") else f"[red]clear failed[/] {res.get('error') or res}")


def cmd_events(a) -> None:
    for ev in reversed(list(world()["chaos_events"].find().sort("ts", -1).limit(a.n))):
        _print_event(ev)


def cmd_score(_a) -> None:
    t = Table(title="Scorecard — Red's faults vs the Hive's incidents", header_style="bold")
    cols = ("run_id", "scenario", "round", "faults_fired", "agent_kills", "incidents_opened", "resolved",
            "ttr_s", "diagnosis_s", "hops", "attempts", "lies_caught", "takeovers", "recall_used")
    for c in cols:
        t.add_column(c.replace("_", " "), overflow="fold")
    for row in score.scorecard():
        t.add_row(*["–" if row.get(c) is None else str(row.get(c)) for c in cols])
    console.print(t)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m chaos.cli", description="Legacy Siege Chaos Monkey (Red)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="fault library and scenarios").set_defaults(fn=cmd_list)
    r = sub.add_parser("run", help="play a seeded scenario")
    r.add_argument("scenario")
    r.add_argument("--round", type=int, default=None)
    r.add_argument("--no-announce", action="store_true", help="don't mirror the run into swarm.stats.current")
    r.set_defaults(fn=cmd_run)
    f = sub.add_parser("fire", help="fire one fault")
    f.add_argument("fault", choices=sorted(faults.FAULTS))
    f.add_argument("target", nargs="?")
    f.add_argument("--params", help="JSON object merged over the fault's defaults")
    f.set_defaults(fn=cmd_fire)
    k = sub.add_parser("kill", help="SIGKILL a bee")
    k.add_argument("role", nargs="?", default="worker")
    k.add_argument("--phase", default=None, help="e.g. step_sent")
    k.add_argument("--agent", default=None, help="exact agent id (overrides role/phase)")
    k.add_argument("--wait", type=float, default=30.0)
    k.set_defaults(fn=cmd_kill)
    sub.add_parser("clear", help="clear all active system faults").set_defaults(fn=cmd_clear)
    e = sub.add_parser("events", help="recent chaos events")
    e.add_argument("-n", type=int, default=20)
    e.set_defaults(fn=cmd_events)
    sub.add_parser("score", help="scorecard per run").set_defaults(fn=cmd_score)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (faults.FaultError, KeyError, json.JSONDecodeError) as e:
        console.print(f"[red]error:[/] {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
