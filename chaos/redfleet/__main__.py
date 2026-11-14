"""``python -m chaos.redfleet``: start every attacker as its own process and restart any that dies
(crash-only, like run.py). ``--agent ID`` runs one attacker in this process."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time

from . import commander, core
from .personas import PERSONAS


def _idle(reason: str) -> None:
    """Stay up doing nothing: run.py restarts exited children and stops the stack on a crash loop."""
    print(f"red fleet: {reason}", flush=True)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    while not stop:
        time.sleep(1)


def run_agent(agent_id: str) -> None:
    if agent_id == commander.COMMANDER_ID:
        c = commander.Commander()
        signal.signal(signal.SIGTERM, c.stop)
        signal.signal(signal.SIGINT, c.stop)
        return c.run()
    from .agent import Attacker

    a = Attacker(PERSONAS[agent_id])
    signal.signal(signal.SIGTERM, a.stop)
    signal.signal(signal.SIGINT, a.stop)
    a.run()


def run_fleet(ids: list[str]) -> None:
    procs: dict[str, subprocess.Popen] = {}
    restarts: dict[str, list[float]] = {i: [] for i in ids}
    stopping = []

    def launch(i: str) -> None:
        procs[i] = subprocess.Popen([sys.executable, "-m", "chaos.redfleet", "--agent", i])
        print(f"red fleet: started {i} pid={procs[i].pid}", flush=True)

    def stop(*_):
        stopping.append(1)
        for p in procs.values():
            if p.poll() is None:
                p.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for i in ids:
        launch(i)
    cmd = None
    if commander.enabled():   # Commander + 2 Scouts: a thread in this supervisor, no extra process
        cmd = commander.start_thread()
        print("red fleet: Red Commander + Hive/Systems Scouts online", flush=True)
    while not stopping:
        time.sleep(1)
        for i in ids:
            if procs[i].poll() is None or stopping:
                continue
            recent = [t for t in restarts[i] if time.monotonic() - t < 60]
            if len(recent) >= 5:
                continue   # crash-looping: leave that attacker down, keep the rest of the fleet
            restarts[i] = recent + [time.monotonic()]
            launch(i)
    if cmd:
        cmd.stop()
    deadline = time.monotonic() + 5
    for p in procs.values():
        try:
            p.wait(max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s redfleet %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m chaos.redfleet")
    ap.add_argument("--agent", choices=sorted(PERSONAS) + [commander.COMMANDER_ID])
    a = ap.parse_args()
    if not core.enabled():
        print("red fleet is off (set RED_FLEET=1 to enable)", flush=True)
        return
    refusal = core.live_db_refusal()
    if refusal:
        return _idle(refusal)
    if a.agent:
        return run_agent(a.agent)
    raw = os.environ.get("RED_FLEET_AGENTS", "").strip()
    ids = [i.strip() for i in raw.split(",") if i.strip() in PERSONAS] if raw else list(PERSONAS)
    run_fleet(ids)


if __name__ == "__main__":
    main()
