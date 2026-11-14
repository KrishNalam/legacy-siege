"""Agent process entrypoint:  python -m swarm.agent <role> <id> [--standby-for <dead agent id>] [--ephemeral]
                       or:  python -m swarm.agent --warm   (dynamic scaling: pre-imported spare, see warm())

One OS process per bee, so a Chaos Monkey SIGKILL is a real crash: nothing survives in memory, and the
replacement continues from MongoDB (leases + LangGraph checkpoints).
"""
from __future__ import annotations

import argparse
import importlib
import shlex
import sys

from .harness import Agent, log

ROLES = {"queen", "scout", "worker", "guard", "waggle", "sentinel", "nurse"}


def specialty_of(agent_id: str) -> str | None:
    # worker-mainframe-1 -> mainframe (a multi-word specialty keeps its dashes: worker-db-repl-2 -> db-repl)
    parts = agent_id.split("-")
    return "-".join(parts[1:-1]) if parts[0] == "worker" and len(parts) >= 3 else None


def warm() -> None:
    """A spare process for dynamic scaling: pay the Python + LangGraph import and the MongoDB connection
    up front (~1.6 s), then block until the supervisor writes one assignment line ("<role> <id> [flags]")
    on stdin and become that agent. EOF (supervisor gone) = exit quietly."""
    for r in sorted(ROLES):
        importlib.import_module(f"swarm.roles.{r}")
    import langgraph.checkpoint.mongodb  # noqa: F401,PLC0415
    from common.db import client  # noqa: PLC0415
    try:
        client().admin.command("ping")
    except Exception:
        pass
    line = sys.stdin.readline()
    if not line.strip():
        return
    main(shlex.split(line))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="swarm.agent")
    ap.add_argument("role", choices=sorted(ROLES))
    ap.add_argument("id")
    ap.add_argument("--standby-for", default=None)
    ap.add_argument("--ephemeral", action="store_true", help="spawned on demand; retires when its work is over")
    a = ap.parse_args(argv)
    standby = a.role == "nurse" or (a.role == "queen" and a.id != "queen-1")
    agent = Agent(a.role, a.id, specialty=specialty_of(a.id), standby=standby, standby_for=a.standby_for,
                  ephemeral=a.ephemeral)
    role = importlib.import_module(f"swarm.roles.{a.role}")
    agent.start()
    try:
        role.run(agent)
    except (SystemExit, KeyboardInterrupt):
        pass
    except Exception as e:  # crash: leave the doc as-is so the Undertaker reaps us like any death
        log(agent.id, f"crashed: {type(e).__name__}: {e}")
        raise
    agent.shutdown()


if __name__ == "__main__":
    if sys.argv[1:] == ["--warm"]:
        warm()
    else:
        main()
