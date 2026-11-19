"""Scorecard: what Red did (world.chaos_events) vs what Blue figured out (swarm.incidents).

Read-only. Red and Blue live in different databases and ``$lookup`` cannot cross
databases, so this runs one aggregation pipeline per side and joins them by
``run_id`` in Python:

* world side: ``runs`` ``$lookup`` ``chaos_events``  -> faults fired per run
* swarm side: ``incidents`` ``$group`` by ``run_id`` -> opened / resolved / TTR /
  diagnosis time / hops / attempts / lies caught / takeovers
"""
from __future__ import annotations

from common.db import swarm, world


def _faults_pipeline(match: dict, limit: int) -> list[dict]:
    return [
        {"$match": match},
        {"$sort": {"started_at": -1}},
        {"$limit": limit},
        {"$lookup": {
            "from": "chaos_events", "localField": "_id", "foreignField": "run_id", "as": "ev",
            "pipeline": [{"$match": {"kind": {"$ne": "decision"}}}, {"$sort": {"ts": 1}}, {"$project": {"fault": 1, "target": 1, "manual": 1, "status": 1}}],
        }},
        {"$project": {
            "_id": 0, "run_id": "$_id", "scenario": 1, "round": 1, "seed": 1, "started_at": 1, "ended_at": 1,
            "faults_fired": {"$size": "$ev"},
            "faults_landed": {"$size": {"$filter": {"input": "$ev", "cond": {"$eq": ["$$this.status", "ok"]}}}},
            "manual_faults": {"$size": {"$filter": {"input": "$ev", "cond": "$$this.manual"}}},
            "agent_kills": {"$size": {"$filter": {"input": "$ev", "cond": {"$and": [
                {"$eq": ["$$this.fault", "agent_kill"]}, {"$eq": ["$$this.status", "ok"]}]}}}},
            "faults": {"$map": {"input": "$ev", "in": {"$concat": ["$$this.fault", "@", "$$this.target"]}}},
        }},
        {"$sort": {"started_at": 1}},
    ]


def _incidents_pipeline(run_ids: list[str]) -> list[dict]:
    return [
        {"$match": {"run_id": {"$in": run_ids}}},
        {"$project": {
            "run_id": 1,
            "resolved": {"$cond": [{"$eq": ["$status", "resolved"]}, 1, 0]},
            "ttr_s": "$metrics.ttr_s", "diagnosis_s": "$metrics.diagnosis_s", "hops": "$metrics.hops",
            "takeover_s": "$metrics.takeover_s",
            "attempts": {"$ifNull": ["$metrics.attempts", {"$sum": {
                "$map": {"input": {"$ifNull": ["$steps", []]}, "in": {"$size": {"$ifNull": ["$$this.attempts", []]}}}}}]},
            # a lie caught = the Guard proved an attempt did NOT apply, so the step got another attempt
            "lies_caught": {"$sum": {"$map": {"input": {"$ifNull": ["$steps", []]}, "in": {
                "$max": [0, {"$subtract": [{"$size": {"$ifNull": ["$$this.attempts", []]}}, 1]}]}}}},
            "takeovers": {"$size": {"$ifNull": ["$handoffs", []]}},
            "recall_used": {"$cond": [{"$eq": ["$recall.used", True]}, 1, 0]},
        }},
        {"$group": {
            "_id": "$run_id",
            "incidents_opened": {"$sum": 1}, "resolved": {"$sum": "$resolved"},
            "ttr_s": {"$avg": "$ttr_s"}, "diagnosis_s": {"$avg": "$diagnosis_s"}, "hops": {"$avg": "$hops"},
            "takeover_s": {"$avg": "$takeover_s"}, "attempts": {"$sum": "$attempts"},
            "lies_caught": {"$sum": "$lies_caught"}, "takeovers": {"$sum": "$takeovers"},
            "recall_used": {"$sum": "$recall_used"},
        }},
        {"$set": {k: {"$round": [f"${k}", 1]} for k in ("ttr_s", "diagnosis_s", "hops", "takeover_s")}},
    ]


EMPTY_BLUE = {"incidents_opened": 0, "resolved": 0, "ttr_s": None, "diagnosis_s": None, "hops": None,
              "takeover_s": None, "attempts": 0, "lies_caught": 0, "takeovers": 0, "recall_used": 0}


def scorecard(run_id: str | None = None, limit: int = 20) -> list[dict]:
    """One row per run (oldest first): Red's faults joined with Blue's incident metrics."""
    match = {"_id": run_id} if run_id else {}
    rows = list(world()["runs"].aggregate(_faults_pipeline(match, limit)))
    blue = {d.pop("_id"): d for d in swarm()["incidents"].aggregate(_incidents_pipeline([r["run_id"] for r in rows]))}
    return [{**r, **EMPTY_BLUE, **blue.get(r["run_id"], {})} for r in rows]
