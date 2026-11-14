"""Swarm supervisor:  python -m swarm.supervisor

Spawns one OS process per agent (`python -m swarm.agent <role> <id>`). It does no hive work itself and
never touches leases: when a child dies (e.g. Chaos SIGKILL), recovery of its WORK is the hive's job
(Undertaker + Nurse via MongoDB). The supervisor only refills the bench: after NURSE_RESPAWN_S it starts
a fresh nurse standby in the dead bee's place. Singleton roles with no standby (guard, sentinel, waggle)
and queens come back in their own role. SIGTERM/SIGINT stops all children cleanly.

Env SWARM_ROSTER="role:id,role:id" overrides the roster (tests).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from common import config

ROOT = Path(__file__).resolve().parent.parent
ROSTER = [
    ("queen", "queen-1"), ("queen", "queen-2"),
    ("scout", "scout-1"), ("scout", "scout-2"),
    ("worker", "worker-mainframe-1"), ("worker", "worker-unix-1"), ("worker", "worker-web-1"),
    ("guard", "guard-1"), ("waggle", "waggle-1"), ("sentinel", "sentinel-1"),
    ("nurse", "nurse-1"), ("nurse", "nurse-2"),
]
SELF_RESPAWN = {"guard", "sentinel", "waggle", "queen", "nurse"}


def roster() -> list[tuple[str, str]]:
    raw = os.environ.get("SWARM_ROSTER", "").strip()
    if not raw:
        return list(ROSTER)
    return [tuple(x.split(":", 1)) for x in raw.split(",") if ":" in x]  # type: ignore[misc]


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} [supervisor] {msg}", flush=True)


class Supervisor:
    def __init__(self):
        self.children: dict[str, tuple[str, subprocess.Popen]] = {}
        self.pending: list[tuple[float, str, str, str | None]] = []  # (when, role, id, standby_for)
        self.stopping = False
        self.next_nurse = 1 + sum(1 for r, _ in roster() if r == "nurse")
        self.retired: set[str] = set()   # extra nurses stopped on a roster restore: never respawned
        self.restore_seen = None
        self.restore_checked = 0.0

    def spawn(self, role: str, agent_id: str, standby_for: str | None = None) -> None:
        cmd = [sys.executable, "-m", "swarm.agent", role, agent_id]
        if standby_for:
            cmd += ["--standby-for", standby_for]
        p = subprocess.Popen(cmd, cwd=ROOT)
        self.children[agent_id] = (role, p)
        log(f"spawned {agent_id} ({role}) pid={p.pid}" + (f" standby for {standby_for}" if standby_for else ""))

    def reap(self) -> None:
        for agent_id, (role, p) in list(self.children.items()):
            code = p.poll()
            if code is None:
                continue
            del self.children[agent_id]
            if self.stopping:
                continue
            if agent_id in self.retired:  # retired on a roster restore: remove its bee from the hive
                self.retired.discard(agent_id)
                try:
                    from common.db import col  # noqa: PLC0415
                    col("agents").delete_one({"_id": agent_id, "pid": p.pid})
                except Exception:
                    pass
                continue
            when = time.monotonic() + config.NURSE_RESPAWN_S
            if role in SELF_RESPAWN:
                self.pending.append((when, role, agent_id, None))
                log(f"{agent_id} exited ({code}); respawning in its own role in {config.NURSE_RESPAWN_S:.0f}s")
            else:
                nid = f"nurse-{self.next_nurse}"
                self.next_nurse += 1
                self.pending.append((when, "nurse", nid, agent_id))
                log(f"{agent_id} exited ({code}); {nid} joins the bench in {config.NURSE_RESPAWN_S:.0f}s")

    def respawn_due(self) -> None:
        now = time.monotonic()
        due = [x for x in self.pending if x[0] <= now]
        self.pending = [x for x in self.pending if x[0] > now]
        for _, role, agent_id, standby_for in due:
            self.spawn(role, agent_id, standby_for)

    def restore_roster(self) -> None:
        """A full demo reset (war room) writes swarm.stats{_id:"roster_reset"}: bring the bench back to the
        original roster — respawn missing roster bees in their own role, retire extra respawned nurses."""
        if time.monotonic() - self.restore_checked < 2.0:
            return
        self.restore_checked = time.monotonic()
        try:
            from common.db import col  # noqa: PLC0415
            doc = col("stats").find_one({"_id": "roster_reset"})
        except Exception:
            return
        if not doc or doc.get("at") == self.restore_seen:
            return
        self.restore_seen = doc.get("at")
        base = roster()
        base_ids = {i for _, i in base}
        self.pending = []
        for agent_id, (_role, p) in list(self.children.items()):
            if agent_id not in base_ids and p.poll() is None:
                self.retired.add(agent_id)
                p.send_signal(signal.SIGTERM)
                log(f"roster restore: retiring {agent_id}")
        for role, agent_id in base:
            if agent_id not in self.children:
                self.spawn(role, agent_id)
        try:
            col("stats").delete_one({"_id": "roster_reset", "at": self.restore_seen})
        except Exception:
            pass

    def stop(self, *_):
        if self.stopping:
            return
        self.stopping = True
        log("stopping all agents")
        for _, p in self.children.values():
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 8
        for _, p in self.children.values():
            try:
                p.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        for role, agent_id in roster():
            self.spawn(role, agent_id)
        while True:
            time.sleep(0.5)
            self.reap()
            self.respawn_due()
            self.restore_roster()


def main() -> None:
    Supervisor().run()


if __name__ == "__main__":
    main()
