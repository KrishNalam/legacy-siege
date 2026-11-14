"""Duration history, ETA estimates and progress events (switch ``PROGRESS_ETA``, default off).

* **History.** Every attack (chaos runner / Red fleet), repair step (swarm) and Gardener stage writes one
  record to the MongoDB **time-series** collection ``action_durations`` (``timeField: ts``,
  ``metaField: meta {action_type, system, actor_role}``, fields ``duration_s``, ``outcome``). Red writes
  ``world.action_durations``, Blue and the Gardener write ``swarm.action_durations`` (Red and Blue never share
  writes). A full reset wipes it; ``next_round`` keeps it, so round 2 estimates from round 1.
* **ETA.** ``estimate()`` runs one aggregation (``$match`` on the meta field, newest 50, ``$group`` with
  ``$percentile``) → median / p90 / K, per action_type+system, falling back to the action_type alone.
* **Progress.** ``emit()`` writes a ``swarm.thoughts`` doc with ``kind:"progress"`` and a structured
  ``progress`` field; the war room already streams thoughts, so the line shows in the activity feed as is.

With the switch off every function here is a no-op (``track()`` still times nothing and writes nothing).
"""
from __future__ import annotations

import logging
import os
import threading
import time

from pymongo.errors import CollectionInvalid, OperationFailure

from .db import col, now, swarm, world

log = logging.getLogger("common.progress")

COLL = "action_durations"
HISTORY = 50          # newest N runs per key feed the estimate
_made: set[str] = set()
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("PROGRESS_ETA", "").strip().lower() in ("1", "true", "yes", "on")


def _db(side: str):
    return world() if side == "world" else swarm()


def ensure(db) -> None:
    """Create ``action_durations`` as a time-series collection (idempotent, cached per DB)."""
    key = f"{db.client.address}/{db.name}"
    if key in _made:
        return
    with _lock:
        if key in _made:
            return
        try:
            db.create_collection(COLL, timeseries={"timeField": "ts", "metaField": "meta", "granularity": "seconds"})
        except (CollectionInvalid, OperationFailure) as e:
            if "already exists" not in str(e) and getattr(e, "code", None) != 48:
                raise
        _made.add(key)


def record(side: str, action_type: str, system: str | None, actor_role: str, duration_s: float,
           outcome: str, **extra) -> None:
    """One finished action -> one time-series measurement. Never raises."""
    if not enabled():
        return
    try:
        db = _db(side)
        ensure(db)
        db[COLL].insert_one({"ts": now(), "meta": {"action_type": action_type, "system": system,
                                                   "actor_role": actor_role},
                             "duration_s": round(max(0.0, float(duration_s)), 3), "outcome": outcome,
                             **{k: v for k, v in extra.items() if v is not None}})
    except Exception:
        log.exception("record %s failed", action_type)


def _pipeline(action_type: str, system: str | None) -> list[dict]:
    match = {"meta.action_type": action_type, "duration_s": {"$type": "number"}}
    if system is not None:
        match["meta.system"] = system
    return [
        {"$match": match},
        {"$sort": {"ts": -1}},
        {"$limit": HISTORY},
        {"$group": {"_id": None, "k": {"$sum": 1},
                    "pct": {"$percentile": {"input": "$duration_s", "p": [0.5, 0.9], "method": "approximate"}}}},
    ]


def estimate(side: str, action_type: str, system: str | None = None) -> dict:
    """``{k, median_s, p90_s, scope}`` from history; ``k == 0`` when nothing is known. Never raises."""
    empty = {"k": 0, "median_s": None, "p90_s": None, "scope": None}
    if not enabled():
        return empty
    try:
        db = _db(side)
        ensure(db)
        for scope in ([system, None] if system is not None else [None]):
            rows = list(db[COLL].aggregate(_pipeline(action_type, scope)))
            if rows and rows[0]["k"]:
                med, p90 = rows[0]["pct"]
                return {"k": rows[0]["k"], "median_s": round(med, 1), "p90_s": round(p90, 1),
                        "scope": f"{action_type}@{scope}" if scope else action_type}
    except Exception:
        log.exception("estimate %s failed", action_type)
    return empty


def eta_text(est: dict) -> str:
    if not est or not est.get("k"):
        return "ETA unknown (no past runs yet)"
    med, p90, k = est["median_s"], est["p90_s"], est["k"]
    spread = f", p90 {p90:.0f} s" if p90 is not None and round(p90) > round(med) else ""
    return f"ETA ~{med:.0f} s{spread} (based on {k} past run{'s' if k != 1 else ''})"


AREAS = {"red": "chaos", "chaos": "chaos", "gardener": "gardener"}   # everything else = the Hive ("bees")


def area_of(role: str) -> str:
    """War-room box this event belongs to: ``gardener`` | ``chaos`` | ``bees``."""
    return AREAS.get(role, "bees")


def emit(agent: str, role: str, text: str, *, action_type: str, system: str | None = None,
         incident: str | None = None, phase: str = "start", step: int | None = None, of: int | None = None,
         eta: dict | None = None, elapsed_s: float | None = None, outcome: str | None = None) -> None:
    """One progress event on the war-room feed (``swarm.thoughts``, ``kind:"progress"``). Never raises."""
    if not enabled():
        return
    done = (step - 1 if phase == "start" else step) if step is not None else None
    pct = round(100.0 * done / of) if done is not None and of else None   # start of step i/n = (i-1)/n done
    area = area_of(role)
    prog = {"area": area, "actor": agent, "role": role, "incident": incident, "action_type": action_type, "system": system, "phase": phase,
            "step": step, "of": of, "pct": pct, "eta_s": (eta or {}).get("median_s"),
            "eta_p90_s": (eta or {}).get("p90_s"), "eta_k": (eta or {}).get("k", 0),
            "elapsed_s": round(elapsed_s, 2) if elapsed_s is not None else None, "outcome": outcome,
            "text": text[:280]}
    try:
        ts = now()
        col("thoughts").insert_one({"agent": agent, "role": role, "incident": incident, "system": system,
                                    "text": text[:280], "kind": "progress", "progress": prog, "ts": ts})
        # latest state per war-room box (one small doc each, streamed like any stats change)
        col("stats").update_one({"_id": f"progress:{area}"},
                                {"$set": {**prog, "ts": ts}, "$inc": {"events": 1}}, upsert=True)
    except Exception:
        log.exception("progress emit failed")


class track:
    """Context manager for one action: ETA + start event on enter, duration record (+ done event) on exit.

        with progress.track("world", "attack:batch_abort", "mainframe", "red", agent="red-saboteur",
                            label="firing batch_abort at mainframe") as t:
            ...; t.outcome = "ok"
    """

    def __init__(self, side: str, action_type: str, system: str | None, actor_role: str, *, agent: str,
                 label: str, incident: str | None = None, step: int | None = None, of: int | None = None,
                 announce: bool = True, done_line: bool = True, record_it: bool = True):
        self.side, self.action_type, self.system, self.role = side, action_type, system, actor_role
        self.agent, self.label, self.incident, self.step, self.of = agent, label, incident, step, of
        self.announce, self.done_line, self.record_it = announce, done_line, record_it
        self.outcome = "ok"
        self.eta: dict = {}
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.monotonic()
        if enabled():
            self.eta = estimate(self.side, self.action_type, self.system)
            if self.announce:
                where = f" (step {self.step}/{self.of})" if self.step is not None and self.of else ""
                emit(self.agent, self.role, f"{self.label}{where} · {eta_text(self.eta)}",
                     action_type=self.action_type, system=self.system, incident=self.incident, phase="start",
                     step=self.step, of=self.of, eta=self.eta)
        return self

    def __exit__(self, exc_type, exc, tb):
        if not enabled():
            return False
        took = time.monotonic() - self.t0
        outcome = "error" if exc_type else self.outcome
        if self.record_it:
            record(self.side, self.action_type, self.system, self.role, took, outcome)
        if self.done_line:
            where = f" (step {self.step}/{self.of})" if self.step is not None and self.of else ""
            emit(self.agent, self.role, f"{self.label}{where}: {outcome} in {took:.1f} s",
                 action_type=self.action_type, system=self.system, incident=self.incident, phase="done",
                 step=self.step, of=self.of, eta=self.eta, elapsed_s=took, outcome=outcome)
        return False


def wipe() -> None:
    """Full reset: forget the history in both DBs."""
    for db in (world(), swarm()):
        try:
            db[COLL].delete_many({})
        except OperationFailure:
            pass
