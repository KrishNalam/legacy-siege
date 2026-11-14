"""Decision log for the Red side's brains (Red Planner, and the Runner's own decisions).

Every decision is a ``world.chaos_events`` document with ``kind: "decision"`` (no ``fault``, no top-level
``target``), so it sits next to the attacks it explains. Consumers that count or draw *attacks* filter it out
with ``NOT_DECISION``.

    log_decision("red-planner", "campaign_brief", run_id="RUN-0001", reason="...", focus="guard")
"""
from __future__ import annotations

import logging

from common.db import next_id, now, world

log = logging.getLogger("chaos.decisions")

KIND = "decision"
NOT_DECISION = {"kind": {"$ne": KIND}}


def log_decision(by: str, decision: str, *, run_id: str | None = None, reason: str = "", **fields) -> dict:
    """Insert one decision document and return it. Never raises (a decision log must not stop a brain)."""
    w = world()
    run = w.runs.find_one({"_id": run_id}) if run_id else None
    doc = {k: v for k, v in fields.items() if k not in ("fault", "target")}
    doc.update({
        "_id": next_id("DEC", db=w), "kind": KIND, "by": by, "decision": decision, "status": "decided",
        "run_id": run_id, "round": (run or {}).get("round"), "scenario": (run or {}).get("scenario"),
        "reason": (reason or "")[:600], "ts": now(), "source": "red-fleet" if by.startswith("red-") else "runner",
    })
    try:
        w.chaos_events.insert_one(doc)
    except Exception:
        log.exception("decision log failed (%s %s)", by, decision)
    return doc


def decisions(run_id: str | None = None, by: str | None = None, limit: int = 50) -> list[dict]:
    q: dict = {"kind": KIND}
    if run_id:
        q["run_id"] = run_id
    if by:
        q["by"] = by
    return list(world().chaos_events.find(q).sort("ts", 1).limit(limit))
