"""Spine analytics (MONGO_SPINE=1 shows it in the Atlas overlay): ONE aggregation round trip answers
"is the Hive actually learning?", computed inside MongoDB, never in Python.

    incidents ─$facet─┬─ learning : $setWindowFields per fault_class, sorted by opened_at
                      │             ($documentNumber = night n, $shift = previous TTR, running min / avg)
                      ├─ rounds   : $group per round with $percentile p50/p90 TTR and $median diagnosis
                      ├─ takeovers: $unwind handoffs → count, median / max takeover seconds
                      └─ totals   : $group → incidents, resolved, recall-driven
    custody   ─$facet─┬─ by_kind  : claim / takeover / release / decision counts (change-stream pre-images)
                      └─ recent   : last takeovers, with the hash-chain tip

Needs MongoDB 7.0+ ($percentile/$median); local atlas-local 8.0 and Atlas 9.0 both have it. Read-only.
"""
from __future__ import annotations

from pymongo.errors import OperationFailure, PyMongoError

from common import config
from common.db import swarm

def incidents_pipeline() -> list[dict]:
    closed = {"$match": {"metrics.ttr_s": {"$type": "number"}}}
    return [{"$facet": {
        "learning": [
            closed,
            {"$setWindowFields": {
                "partitionBy": "$fault_class", "sortBy": {"opened_at": 1},
                "output": {
                    "night": {"$documentNumber": {}},
                    "prev_ttr_s": {"$shift": {"output": "$metrics.ttr_s", "by": -1}},
                    "prev_diag_s": {"$shift": {"output": "$metrics.diagnosis_s", "by": -1}},
                    "best_ttr_s": {"$min": "$metrics.ttr_s", "window": {"documents": ["unbounded", "current"]}},
                    "avg3_ttr_s": {"$avg": "$metrics.ttr_s", "window": {"documents": [-2, "current"]}},
                }}},
            {"$project": {"_id": 1, "fault_class": 1, "round": 1, "night": 1, "opened_at": 1,
                          "ttr_s": "$metrics.ttr_s", "diagnosis_s": "$metrics.diagnosis_s", "hops": "$metrics.hops",
                          "recall": "$recall.used", "prev_ttr_s": 1, "prev_diag_s": 1, "best_ttr_s": 1,
                          "avg3_ttr_s": {"$round": ["$avg3_ttr_s", 2]},
                          "speedup": {"$cond": [{"$and": [{"$gt": ["$prev_ttr_s", 0]}, {"$gt": ["$metrics.ttr_s", 0]}]},
                                                {"$round": [{"$divide": ["$prev_ttr_s", "$metrics.ttr_s"]}, 2]},
                                                None]}}},
            {"$sort": {"opened_at": -1}}, {"$limit": 12},
        ],
        "rounds": [
            closed, {"$match": {"round": {"$ne": None}}},
            {"$group": {"_id": "$round", "n": {"$sum": 1},
                        "ttr_p": {"$percentile": {"input": "$metrics.ttr_s", "p": [0.5, 0.9], "method": "approximate"}},
                        "diag_median_s": {"$median": {"input": "$metrics.diagnosis_s", "method": "approximate"}},
                        "hops_avg": {"$avg": "$metrics.hops"}}},
            {"$project": {"_id": 0, "round": "$_id", "n": 1, "ttr_p50_s": {"$arrayElemAt": ["$ttr_p", 0]},
                          "ttr_p90_s": {"$arrayElemAt": ["$ttr_p", 1]}, "diag_median_s": 1,
                          "hops_avg": {"$round": ["$hops_avg", 2]}}},
            {"$sort": {"round": 1}},
        ],
        "takeovers": [
            {"$unwind": "$handoffs"},
            {"$group": {"_id": None, "n": {"$sum": 1},
                        "median_s": {"$median": {"input": "$handoffs.takeover_s", "method": "approximate"}},
                        "max_s": {"$max": "$handoffs.takeover_s"},
                        "skipped_steps": {"$sum": {"$size": {"$ifNull": ["$handoffs.skipped_steps", []]}}}}},
            {"$project": {"_id": 0}},
        ],
        "totals": [
            {"$group": {"_id": None, "incidents": {"$sum": 1},
                        "resolved": {"$sum": {"$cond": [{"$eq": ["$status", "resolved"]}, 1, 0]}},
                        "recall_driven": {"$sum": {"$cond": [{"$eq": ["$recall.used", True]}, 1, 0]}}}},
            {"$project": {"_id": 0}},
        ],
    }}]


def custody_pipeline() -> list[dict]:
    return [{"$facet": {
        "by_kind": [{"$group": {"_id": "$kind", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}],
        "recent": [{"$match": {"kind": "takeover"}}, {"$sort": {"ts": -1}}, {"$limit": 5},
                   {"$project": {"_id": 0, "doc_id": 1, "from_owner": 1, "to_owner": 1, "gen_before": 1,
                                 "gen_after": 1, "lease_expired": 1, "pre_image": 1, "seq": 1, "ts": 1,
                                 "hash": {"$substrCP": ["$hash", 0, 12]}}}],
    }}]


def analytics() -> dict:
    out: dict = {"enabled": config.MONGO_SPINE,
                 "pipelines": {"incidents": incidents_pipeline(), "custody": custody_pipeline()}}
    try:
        inc = next(swarm().incidents.aggregate(incidents_pipeline()), {})
        out.update(learning=inc.get("learning", []), rounds=inc.get("rounds", []),
                   takeovers=(inc.get("takeovers") or [{}])[0], totals=(inc.get("totals") or [{}])[0])
        cus = next(swarm().custody.aggregate(custody_pipeline()), {})
        out["custody"] = {"by_kind": {r["_id"]: r["n"] for r in cus.get("by_kind", [])},
                          "recent": cus.get("recent", [])}
        try:
            colls = {c["name"]: c.get("options", {}) for c in swarm().list_collections(
                filter={"name": {"$in": ["tasks", "incidents", "approvals"]}})}
            out["pre_images"] = {n: bool((o.get("changeStreamPreAndPostImages") or {}).get("enabled"))
                                 for n, o in colls.items()}
        except PyMongoError:
            out["pre_images"] = {}
    except (OperationFailure, PyMongoError) as e:  # e.g. an old server without $percentile
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out
