"""The initial picture a browser gets right after its change stream is attached.

Built with the async driver straight from MongoDB — the war room keeps no state of its own.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING

from common import config

from .bson_json import to_json
from .stream import Hub, db_names


async def _find(coll, filt=None, sort=None, limit=0, projection=None) -> list[dict]:
    cur = coll.find(filt or {}, projection)
    if sort:
        cur = cur.sort(sort)
    if limit:
        cur = cur.limit(limit)
    return await cur.to_list(None)


async def _recent_logs(world) -> list[dict]:
    """Last 4 log lines per system: $sort → $group/$push → $slice (one round trip)."""
    pipeline = [
        {"$sort": {"ts": -1}},
        {"$limit": 300},
        {"$group": {"_id": "$system", "lines": {"$push": {"ts": "$ts", "level": "$level", "code": "$code",
                                                          "line": "$line"}}}},
        {"$project": {"lines": {"$slice": ["$lines", 4]}}},
    ]
    cur = await world.system_logs.aggregate(pipeline)
    return await cur.to_list(None)


async def build(hub: Hub) -> dict:
    w, s = hub.world(), hub.swarm()
    active, recent = await asyncio.gather(
        _find(s.incidents, {"active": True}, [("opened_at", DESCENDING)], 10),
        _find(s.incidents, {}, [("opened_at", DESCENDING)], 10),
    )
    incidents = {i["_id"]: i for i in recent + active}
    inc_ids = list(incidents)
    (systems, agents, tasks, thoughts, chaos, approvals_pending, approvals_recent, upgrades, ledger, playbooks,
     logs, runs, current, counts, roster) = await asyncio.gather(
        _find(w.systems, sort=[("_id", ASCENDING)]),
        _find(s.agents, sort=[("_id", ASCENDING)]),
        _find(s.tasks, {"incident": {"$in": inc_ids}}),
        _find(s.thoughts, sort=[("ts", DESCENDING)], limit=50),
        _find(w.chaos_events, {"kind": {"$ne": "decision"}}, [("ts", DESCENDING)], 20),
        _find(s.approvals, {"status": "pending"}, [("ts", ASCENDING)]),
        _find(s.approvals, sort=[("ts", DESCENDING)], limit=5),
        _find(s.upgrade_requests, sort=[("ts", DESCENDING)], limit=5),
        _find(s.ledger, sort=[("ts", DESCENDING)], limit=30),
        _find(s.playbooks, sort=[("created_at", DESCENDING)], limit=10),
        _recent_logs(w),
        _find(w.runs, sort=[("started_at", ASCENDING)], limit=50),
        s.stats.find_one({"_id": "current"}),
        hub.counts(),
        _find(s.roster, sort=[("_id", ASCENDING)]),  # Comms live roster (SWARM_COMMS=1; else empty)
    )
    approvals = {a["_id"]: a for a in approvals_recent + approvals_pending}
    run = None
    if current and current.get("run_id"):
        run = next((r for r in runs if r["_id"] == current["run_id"]), None)
    return to_json({
        "type": "snapshot",
        "systems": systems, "agents": agents,
        "incidents": sorted(incidents.values(), key=lambda i: i.get("opened_at") or datetime.min.replace(tzinfo=timezone.utc)),
        "tasks": tasks, "thoughts": list(reversed(thoughts)), "chaos_events": list(reversed(chaos)),
        "approvals": list(approvals.values()), "upgrades": list(reversed(upgrades)),
        "ledger": list(reversed(ledger)), "playbooks": list(reversed(playbooks)),
        "logs": {g["_id"]: list(reversed(g["lines"])) for g in logs if g.get("_id")},
        "runs": runs, "roster": roster, "current": current, "run": run, "counts": counts,
        "meta": {"dbs": db_names(), "stream": "live" if hub.connected else "down",
                 "embed_model": config.VOYAGE_MODEL if config.VOYAGE_API_KEY else "local-hash-v1",
                 "server_time": datetime.now(timezone.utc)},
    })
