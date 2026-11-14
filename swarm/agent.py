"""Agent process entrypoint:  python -m swarm.agent <role> <id> [--standby-for <dead agent id>]

One OS process per bee, so a Chaos Monkey SIGKILL is a real crash: nothing survives in memory, and the
replacement continues from MongoDB (leases + LangGraph checkpoints).
"""
from __future__ import annotations

import argparse
import importlib

from .harness import Agent, log

ROLES = {"queen", "scout", "worker", "guard", "waggle", "sentinel", "nurse"}


def specialty_of(agent_id: str) -> str | None:
    # worker-mainframe-1 -> mainframe
    parts = agent_id.split("-")
    return parts[1] if parts[0] == "worker" and len(parts) >= 3 else None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="swarm.agent")
    ap.add_argument("role", choices=sorted(ROLES))
    ap.add_argument("id")
    ap.add_argument("--standby-for", default=None)
    a = ap.parse_args(argv)
    standby = a.role == "nurse" or (a.role == "queen" and a.id != "queen-1")
    agent = Agent(a.role, a.id, specialty=specialty_of(a.id), standby=standby, standby_for=a.standby_for)
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
    main()
