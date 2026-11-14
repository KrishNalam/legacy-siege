"""Seeded demo scenarios -> world.chaos_scenarios.

A scenario is a fixed list of ``(t_offset_s, fault, target, params)`` steps plus a
seed. Steps are stored fully concrete, so the same scenario always replays the
same timeline (rehearsal == stage). The seed is stored with the scenario and
stamped on every chaos event fired by its runs.
"""
from __future__ import annotations

from common.db import world

from . import faults

SCENARIOS: list[dict] = [
    {
        "_id": "bank-night-1", "seed": 1001,
        "title": "Bank night 1 — the cascade (and the mainframe lies once)",
        "steps": [
            {"t": 0, "fault": "lying_success", "target": "mainframe", "params": {"count": 1}},
            {"t": 1, "fault": "batch_abort", "target": "mainframe", "params": {}},
        ],
    },
    {
        "_id": "bank-night-2", "seed": 1002,
        "title": "Bank night 2 — same fault class, no lies (round-2 timing vs recall)",
        "steps": [{"t": 0, "fault": "batch_abort", "target": "mainframe", "params": {}}],
    },
    {
        "_id": "bank-night-3", "seed": 1003,
        "title": "Bank night 3 — replay after the Gardener's upgrade (expect no incident)",
        "steps": [{"t": 0, "fault": "batch_abort", "target": "mainframe", "params": {}}],
    },
    {"_id": "free-play", "seed": 0, "title": "Free play — manual chaos buttons only", "steps": []},
]


def _validate(scn: dict) -> None:
    for step in scn["steps"]:
        faults.resolve_target(faults.get(step["fault"]), step["target"])


def seed() -> list[str]:
    """Idempotently upsert the built-in scenarios (replace_one upsert = same doc every time)."""
    coll = world()["chaos_scenarios"]
    for scn in SCENARIOS:
        _validate(scn)
        coll.replace_one({"_id": scn["_id"]}, scn, upsert=True)
    return [s["_id"] for s in SCENARIOS]


def get(name: str) -> dict:
    """Load a scenario from MongoDB, seeding the built-ins on first use."""
    doc = world()["chaos_scenarios"].find_one({"_id": name})
    if doc is None and any(s["_id"] == name for s in SCENARIOS):
        seed()
        doc = world()["chaos_scenarios"].find_one({"_id": name})
    if doc is None:
        raise KeyError(f"unknown scenario {name!r}")
    doc["steps"] = sorted(doc.get("steps", []), key=lambda s: s["t"])  # stable: ties keep stored order
    return doc


def list_all() -> list[dict]:
    ids = [s["_id"] for s in SCENARIOS]
    if world()["chaos_scenarios"].count_documents({"_id": {"$in": ids}}) < len(ids):
        seed()
    return list(world()["chaos_scenarios"].find().sort("_id", 1))
