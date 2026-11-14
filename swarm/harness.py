"""Common harness every agent process runs on.

* registers the agent in swarm.agents (pid, role, sprite target) and heartbeats `last_seen`
* renews the leases this process holds, conditionally on {owner, generation}; a failed renew marks the
  lease lost and the acting code stops at its next `check()` (no split brain)
* `think()` writes one legible sentence to swarm.thoughts and paces the demo
* `checkpointer()` is the shared LangGraph MongoDBSaver (db = SWARM_DB)
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from datetime import datetime

from common import config, events, llm
from common.db import client, col, now

from . import leases, settings
from .http import Systems

_saver = None


def checkpointer():
    """One MongoDBSaver per process; checkpoints live in swarm.checkpoints / checkpoint_writes."""
    global _saver
    if _saver is None:
        from langgraph.checkpoint.mongodb import MongoDBSaver

        _saver = MongoDBSaver(client(), db_name=config.SWARM_DB)
    return _saver


def log(agent_id: str, msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} [{agent_id}] {msg}", flush=True)


class Agent:
    def __init__(self, role: str, agent_id: str, specialty: str | None = None,
                 standby: bool = False, standby_for: str | None = None):
        self.id, self.role, self.specialty = agent_id, role, specialty
        self.standby, self.standby_for = standby, standby_for
        self.pid = os.getpid()
        self.systems = Systems()
        self.stop = threading.Event()
        self._held: dict[str, tuple[str, object, int]] = {}  # key -> (collection, _id, generation)
        self._lost: set[str] = set()
        self._lock = threading.Lock()
        self._hb: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def idle_spot(self) -> tuple[str, str]:
        return ("standby", "bench") if self.standby else ("idle", "hive")

    def register(self) -> None:
        status, target = self.idle_spot()
        col("agents").update_one({"_id": self.id}, {
            "$set": {"role": self.role, "specialty": self.specialty, "status": status, "target": target,
                     "incident": None, "task": None, "phase": None, "pid": self.pid, "last_seen": now(),
                     "standby_for": self.standby_for, "started_at": now(), "died_at": None},
            "$inc": {"generation": 1},
        }, upsert=True)

    def start(self) -> "Agent":
        self.register()
        signal.signal(signal.SIGTERM, self._on_term)
        self._hb = threading.Thread(target=self._heartbeat, name="heartbeat", daemon=True)
        self._hb.start()
        log(self.id, f"up pid={self.pid} role={self.role}")
        return self

    def _on_term(self, *_):
        if self.stop.is_set():  # second SIGTERM (supervisor + run.py's process-group kill): already leaving
            return
        self.stop.set()
        raise SystemExit(0)

    def shutdown(self) -> None:
        """Clean exit (SIGTERM): hand back leases immediately and grey out."""
        self.stop.set()
        with self._lock:
            held = dict(self._held)
            self._held.clear()
        for _k, (c, _id, gen) in held.items():
            col(c).update_one({"_id": _id, "owner": self.id, "generation": gen},
                              {"$set": {"lease_until": leases.EPOCH}})
        col("agents").update_one({"_id": self.id, "pid": self.pid},
                                 {"$set": {"status": "dead", "phase": "shutdown", "died_at": now()}})
        log(self.id, "shut down")

    # ------------------------------------------------------------------ heartbeat + leases
    def _heartbeat(self) -> None:
        while not self.stop.is_set():
            try:
                self.beat()
            except Exception as e:  # never let the heartbeat thread die
                log(self.id, f"heartbeat error: {e}")
            self.stop.wait(config.HEARTBEAT_S)

    def beat(self) -> None:
        doc = col("agents").find_one_and_update({"_id": self.id, "pid": self.pid},
                                                {"$set": {"last_seen": now()}})
        if doc is None:
            other = col("agents").find_one({"_id": self.id}, {"pid": 1})
            if other is None:  # a reset wiped swarm.agents: just re-register
                self.register()
                return
            log(self.id, f"agent id now owned by pid {other.get('pid')}; this process is a zombie, exiting")
            os._exit(3)
        if doc.get("status") == "dead":
            # The Undertaker declared us dead (we stalled). Our leases were released and may already be
            # fenced by a nurse: drop them all and rejoin as a fresh idle bee.
            with self._lock:
                self._lost.update(self._held)
                self._held.clear()
            status, target = self.idle_spot()
            col("agents").update_one({"_id": self.id, "pid": self.pid},
                                     {"$set": {"status": status, "target": target, "phase": None, "died_at": None}})
        with self._lock:
            held = dict(self._held)
        for key, (c, _id, gen) in held.items():
            if not leases.renew(col(c), _id, self.id, gen):
                log(self.id, f"lost lease {key}")
                with self._lock:
                    self._held.pop(key, None)
                    self._lost.add(key)

    def hold(self, coll: str, _id, generation: int) -> str:
        key = f"{coll}/{_id}"
        with self._lock:
            self._held[key] = (coll, _id, generation)
            self._lost.discard(key)
        return key

    def drop(self, coll: str, _id) -> None:
        with self._lock:
            self._held.pop(f"{coll}/{_id}", None)

    def check(self, coll: str, _id) -> None:
        """Raise LeaseLost if the heartbeat found this lease gone. Call before every action."""
        key = f"{coll}/{_id}"
        with self._lock:
            lost = key in self._lost
        if lost:
            raise leases.LeaseLost(key)
        if self.stop.is_set():
            raise SystemExit(0)

    # ------------------------------------------------------------------ narration + sprite
    def set(self, **fields) -> None:
        """Update this agent's doc: target (sprite position), phase, status, incident, task."""
        r = col("agents").update_one({"_id": self.id}, {"$set": {**fields, "last_seen": now()}})
        if r.matched_count == 0:  # a full demo reset wiped swarm.agents: re-register, then apply
            self.register()
            col("agents").update_one({"_id": self.id}, {"$set": {**fields, "last_seen": now()}})

    def rest(self) -> None:
        status, target = self.idle_spot()
        self.set(status=status, target=target, phase=None, incident=None, task=None)

    def think(self, text: str, kind: str = "thought", incident: str | None = None, system: str | None = None,
              pause: bool = True, polish: bool = False) -> None:
        t0 = time.monotonic()
        if polish and llm.llm_available():
            better = llm.complete(
                "Rewrite as one short, plain sentence an SRE would say. Keep every id and number. No preamble.",
                text, max_tokens=80)
            if better:
                text = better.splitlines()[0]
        events.think(self.id, self.role, text, incident=incident, system=system, kind=kind)
        log(self.id, f"({kind}) {text}")
        if pause:  # LLM time counts against the deliberate pause, so a key never slows the demo down
            self.nap(config.pace(settings.THINK_S) - (time.monotonic() - t0))

    def nap(self, seconds: float) -> None:
        if seconds > 0 and self.stop.wait(seconds):
            raise SystemExit(0)


def retrying(agent: Agent, fn, what: str, attempts: int = 5):
    """Run a graph driver; on an unexpected error (e.g. a systems API blip) resume it from its last
    MongoDB checkpoint instead of abandoning held work. Lease loss and shutdown propagate immediately."""
    for i in range(attempts):
        try:
            return fn()
        except (SystemExit, leases.LeaseLost):
            raise
        except Exception as e:
            log(agent.id, f"{what}: {type(e).__name__}: {e} — resuming from checkpoint ({i + 1}/{attempts})")
            if i == attempts - 1:
                raise
            agent.nap(1.0)


def main_loop(agent: Agent, tick, interval: float) -> None:
    """Run `tick()` forever (until SIGTERM), surviving transient errors."""
    while not agent.stop.is_set():
        t0 = time.monotonic()
        try:
            tick()
        except SystemExit:
            raise
        except leases.LeaseLost as e:
            log(agent.id, f"lease lost, stepping back: {e}")
            agent.rest()
        except Exception as e:
            log(agent.id, f"error: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc(file=sys.stdout)
            agent.nap(1.0)
        agent.nap(max(0.0, interval - (time.monotonic() - t0)))
