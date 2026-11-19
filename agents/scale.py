"""Dynamic swarm scaling (switch SWARM_DYNAMIC=1, default off: the fixed roster is unchanged).

With the switch on there is no standing pool of scouts and workers:
  * the Queen asks for a Scout per new incident when no idle scout is free;
  * a Scout asks for a Worker of the specialty a task needs (a diagnose hop on `unix`, a fix step on
    `mainframe`, ...) when no idle worker of that kind is free;
  * spawned bees retire themselves once their incident is closed (or after SWARM_IDLE_S idle).

A request is a document in swarm.spawn_requests; the supervisor (another process) watches that collection,
starts the agent (from a pre-warmed process, so a spawn costs milliseconds, not the ~1.6 s Python/LangGraph
cold start) and records the result on the request. Everything goes through MongoDB, so it works across
processes and the war room sees bees appear (agents insert) and retire (agents delete) live.

    swarm.spawn_requests {_id: "SPW-0001", role, specialty, incident, task, reason, requested_by,
                          status: "pending"|"spawned"|"cancelled", agent_id, pid, warm, note, ts, spawned_at, spawn_ms}

Specialty is free-form: any new task specialty gets its own worker kind (`worker-<specialty>-<n>`).
"""
from __future__ import annotations

import os
import time
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from common.config import env_float
from common.db import col, next_id, now

from . import settings


def enabled() -> bool:
    return os.environ.get("SWARM_DYNAMIC", "").strip().lower() in ("1", "true", "yes", "on")


MAX_AGENTS = int(env_float("SWARM_MAX_AGENTS", 24))       # hard ceiling on live agent processes (box safety)
WARM = int(env_float("SWARM_WARM", 2))                    # pre-imported spare processes kept ready
IDLE_S = env_float("SWARM_IDLE_S", 15)                    # a spawned bee with no work this long retires
RETIRE_AFTER_S = env_float("SWARM_RETIRE_AFTER_S", 3)     # ...or this long after its incident closed
SPAWN_GRACE_S = env_float("SWARM_SPAWN_GRACE_S", 8)       # nurses leave work alone while its spawn is under way
REQUEST_TTL_S = env_float("SWARM_REQUEST_TTL_S", 30)      # a request not served by then is cancelled


def _fresh() -> object:
    return now() - timedelta(seconds=settings.DEAD_AFTER_S)


# ---------------------------------------------------------------------------------------------- requests
def request(role: str, by: str, reason: str, specialty: str | None = None, incident: str | None = None,
            task: str | None = None) -> str | None:
    """Ask the supervisor for a new agent. Idempotent per (role, task/incident): a re-run node after a
    resume does not ask twice. Returns the request id (or None if one is already open)."""
    key = {"role": role, "status": {"$in": ["pending", "spawned"]}}
    if task:
        key["task"] = task
    elif incident:
        key["incident"] = incident
    if (task or incident) and col("spawn_requests").count_documents(key, limit=1):
        return None
    for _ in range(3):
        rid = next_id("SPW")
        try:
            col("spawn_requests").insert_one({
                "_id": rid, "role": role, "specialty": specialty, "incident": incident, "task": task,
                "reason": reason, "requested_by": by, "status": "pending", "agent_id": None, "pid": None,
                "warm": None, "note": None, "ts": now(), "spawned_at": None, "spawn_ms": None})
            return rid
        except DuplicateKeyError:  # counters were reset while old requests survived: take the next id
            continue
    return None


def _idle(role: str, **extra) -> int:
    return col("agents").count_documents({"role": role, "status": "idle", "phase": {"$ne": "retiring"},
                                          "last_seen": {"$gte": _fresh()}, **extra})


def _in_flight(role: str, **extra) -> int:
    """Spawns asked for but whose agent has not taken work yet."""
    cutoff = now() - timedelta(seconds=SPAWN_GRACE_S)
    return col("spawn_requests").count_documents({"role": role, "status": {"$in": ["pending", "spawned"]},
                                                  "ts": {"$gte": cutoff}, **extra})


def need_worker(specialty: str) -> bool:
    """True when the open tasks of this specialty outnumber idle workers plus spawns already under way."""
    open_tasks = col("tasks").count_documents({"status": "open", "specialty": specialty})
    return open_tasks > _idle("worker", specialty=specialty) + _in_flight("worker", specialty=specialty)


def ensure_worker(by: str, task: dict) -> str | None:
    """Scout side: the task needs a `task.specialty` worker; spawn one unless an idle one is free."""
    if not enabled() or not need_worker(task["specialty"]):
        return None
    return request("worker", by, f"{task['kind']} {task.get('system') or ''} for {task['incident']}".strip(),
                   specialty=task["specialty"], incident=task["incident"], task=task["_id"])


def ensure_scout(by: str, inc_id: str) -> str | None:
    """Queen side: one scout per incident; spawn one unless an idle scout is free."""
    if not enabled():
        return None
    unowned = col("incidents").count_documents({"active": True, "status": "open"})
    if unowned <= _idle("scout") + _in_flight("scout"):
        return None
    return request("scout", by, f"owns {inc_id}", incident=inc_id)


def spawn_pending(task_id: str | None = None, incident: str | None = None) -> bool:
    """Nurse side: a spawn for this work is under way, so don't steal it (until SPAWN_GRACE_S has passed)."""
    if not enabled():
        return False
    q = {"status": {"$in": ["pending", "spawned"]}, "ts": {"$gte": now() - timedelta(seconds=SPAWN_GRACE_S)}}
    if task_id:
        q["task"] = task_id
    else:
        q["incident"] = incident
    return col("spawn_requests").count_documents(q, limit=1) > 0


# ---------------------------------------------------------------------------------------------- retirement
def _work_waiting(agent) -> bool:
    if agent.role == "worker":
        return col("tasks").count_documents({"status": "open", "specialty": agent.specialty}, limit=1) > 0
    if agent.role == "scout":
        return col("incidents").count_documents({"active": True, "status": "open",
                                                 "assigned_to": {"$in": [agent.id, None]}}, limit=1) > 0
    if agent.role == "nurse":
        return col("tasks").count_documents({"status": "open"}, limit=1) > 0
    return False


def maybe_retire(agent) -> None:
    """Called by the main loop of a spawned (ephemeral) bee between ticks. Raises SystemExit to retire."""
    if not agent.ephemeral or agent.holding():
        return
    t = time.monotonic()
    idle = t - agent.last_busy
    if idle < RETIRE_AFTER_S or t - agent._retire_checked < 1.0:
        return
    agent._retire_checked = t
    why = None
    if idle >= IDLE_S:
        why = f"idle {idle:.0f}s"
    elif agent.last_incident:
        inc = col("incidents").find_one({"_id": agent.last_incident}, {"active": 1, "status": 1})
        if not inc or not inc.get("active"):
            why = f"{agent.last_incident} {(inc or {}).get('status', 'closed')}"
    if not why:
        return
    # Step out of the idle pool first, THEN look for work: a scout that counted us before this write
    # left a task we will see below; one that counts after it spawns a fresh worker. No task is stranded.
    r = col("agents").update_one({"_id": agent.id, "pid": agent.pid, "status": {"$in": ["idle", "standby"]}},
                                 {"$set": {"phase": "retiring"}})
    if not r.matched_count:
        return
    if _work_waiting(agent):
        col("agents").update_one({"_id": agent.id, "pid": agent.pid}, {"$set": {"phase": None}})
        agent.last_busy = time.monotonic()
        return
    agent.retire(why)
