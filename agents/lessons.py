"""Failure memory ("lessons"): what DIDN'T work, remembered in MongoDB next to the playbooks.

Playbooks remember the fix that worked. Lessons remember the attempts that failed verification, so the
next incident doesn't repeat them:

* ``lied``        an op ACKed ``applied:true`` but the Guard's read-back showed nothing changed (ACK ≠ APPLIED)
* ``refused``     the system refused the op (``accepted:false``)
* ``not_applied`` accepted, but the effect never landed inside the slow-apply window
* ``no_ack``      no answer from the system (transport error / worker died before the POST)
* ``misfit``      a wrong hypothesis: a recalled playbook didn't fit what its root system showed

One document per (system, version, op-or-playbook, kind) in ``agents.lessons``; a repeat failure bumps
``count`` and appends to ``history``. Every lesson carries the ``system_version`` it was learned on: when the
Sentinel sees a version bump (e.g. the Gardener deployed an upgrade) older lessons go ``stale`` exactly like
playbooks, and a stale or version-mismatched lesson is never applied — the plan says so instead.

On recall/plan the Scout reads the lessons that match its plan (annotating each step in the incident
document, so a Nurse that takes over reads the same memory) and narrates them; the Guard rejects a repeat
lie without waiting out the grace period; a known-misfit playbook is skipped in favour of the next hit.

Behind ``FAILURE_MEMORY=1`` (default off). With the switch off nothing here is called.
"""
from __future__ import annotations

import os
import re

from pymongo import ASCENDING, DESCENDING

from common.db import col, now

KINDS = ("lied", "refused", "not_applied", "no_ack", "misfit")
STEP_KINDS = ("lied", "refused", "not_applied", "no_ack")
HISTORY_KEEP = 10


def enabled() -> bool:
    """Read at call time so tests (and a restarted agent) pick up the switch without re-imports."""
    return os.environ.get("FAILURE_MEMORY", "").strip().lower() in ("1", "true", "yes", "on")


def lesson_id(system: str, version, subject: str, kind: str) -> str:
    """Readable, deterministic id: the same failure on the same version is ONE lesson (count goes up)."""
    return f"LS:{system}:v{version}:{subject}:{kind}"


def classify(evidence: str, response: dict | None) -> str:
    """Map the Guard's rejection reason (and the recorded ACK) to a lesson kind."""
    ev = evidence or ""
    if ev.startswith("ACK ≠ APPLIED"):
        return "lied"
    if (response or {}).get("accepted") is False or " refused " in f" {ev} ":
        return "refused"
    if "never applied" in ev:
        return "not_applied"
    return "no_ack"


def _why(kind: str, op: str, system: str, evidence: str) -> str:
    facts = re.sub(r"^ACK ≠ APPLIED:\s*", "", evidence or "").strip()
    return {
        "lied": f"{system} ACKed {op} with applied:true but read-back showed nothing changed ({facts})",
        "refused": f"{system} refused {op}: {facts}",
        "not_applied": f"{op} was accepted by {system} but never took effect: {facts}",
        "no_ack": f"no ACK from {system} for {op}: {facts}",
    }.get(kind, facts)[:300]


# ---------------------------------------------------------------------------------------------- write
def _upsert(_id: str, base: dict, event: dict) -> dict:
    t = now()
    col("lessons").update_one(
        {"_id": _id},
        {"$setOnInsert": {**base, "first_seen": t, "stale": False, "uses": 0},
         "$set": {"last_seen": t, "why": event["why"], "evidence": event.get("evidence"),
                  "last_incident": event.get("incident")},
         "$inc": {"count": 1},
         "$addToSet": {"incidents": event.get("incident")},
         "$push": {"history": {"$each": [{**event, "ts": t}], "$slice": -HISTORY_KEEP}}},
        upsert=True)
    return col("lessons").find_one({"_id": _id})


def record_failure(incident: str, step: dict, action_id: str, evidence: str, agent: str, version: int,
                   fault_class: str | None = None) -> dict:
    """Guard: an attempt failed verification -> durable lesson tagged with the system's current version."""
    system, op = step["system"], step["op"]
    kind = classify(evidence, step.get("response"))
    why = _why(kind, op, system, evidence)
    base = {"kind": kind, "system": system, "system_version": version, "op": op,
            "params": dict(step.get("params") or {}), "fault_class": fault_class, "playbook": None}
    event = {"incident": incident, "step_id": step["step_id"], "action_id": action_id, "agent": agent,
             "why": why, "evidence": (evidence or "")[:300], "response": step.get("response")}
    return _upsert(lesson_id(system, version, op, kind), base, event)


def record_misfit(incident: str, playbook: dict, symptom_text: str, observed: dict, agent: str,
                  version: int) -> dict:
    """Scout: a recalled playbook didn't fit (wrong hypothesis) -> skip it next time for these symptoms."""
    system = playbook["system"]
    seen = observed.get("fault_class") or "no fault"
    why = (f"{playbook['playbook_id']} predicted {playbook['fault_class']} on {system} but {system} "
           f"showed {seen}")[:300]
    base = {"kind": "misfit", "system": system, "system_version": version, "op": None, "params": {},
            "fault_class": playbook.get("fault_class"), "playbook": playbook["playbook_id"],
            "symptom_text": symptom_text}
    event = {"incident": incident, "agent": agent, "why": why,
             "evidence": "; ".join(observed.get("facts") or [])[:300], "observed_fault": observed.get("fault_class")}
    return _upsert(lesson_id(system, version, playbook["playbook_id"], "misfit"), base, event)


def note_recovery(incident: str, step: dict, action_id: str) -> int:
    """A later attempt of a step that failed in this incident was verified: remember what worked."""
    if len(step.get("attempts") or []) < 2:
        return 0
    return col("lessons").update_many(
        {"incidents": incident, "system": step["system"], "op": step["op"], "kind": {"$in": list(STEP_KINDS)}},
        {"$set": {"recovered_by": f"retry {action_id} verified by read-back", "recovered_at": now()}}).modified_count


def mark_stale(system: str, version: int) -> int:
    """Sentinel: `system` is now `version` -> every lesson learned on an older version goes stale."""
    return col("lessons").update_many(
        {"system": system, "stale": False, "system_version": {"$lt": version}},
        {"$set": {"stale": True, "staled_at": now(), "stale_reason": f"{system} upgraded to v{version}"}}
    ).modified_count


def _used(ids: list[str], incident: str) -> None:
    if ids:
        col("lessons").update_many({"_id": {"$in": ids}},
                                   {"$inc": {"uses": 1}, "$set": {"last_used_at": now()},
                                    "$addToSet": {"used_in": incident}})


def _versions(version_fn):
    cache: dict = {}

    def get(sys):
        if sys not in cache:
            try:
                cache[sys] = version_fn(sys)
            except Exception:
                cache[sys] = None
        return cache[sys]
    return get


# ---------------------------------------------------------------------------------------------- read: plan
def for_plan(plan_steps: list[dict], version_fn, incident: str) -> tuple[list[dict], list[str]]:
    """Annotate plan steps (in place) with the lessons that match them and return (applied, narration).

    A lesson applies to a step with the same system + op when it was learned on the system's CURRENT
    version and isn't stale. Others are reported as outdated, never applied."""
    pairs = {(s["system"], s["op"]) for s in plan_steps}
    if not pairs:
        return [], []
    found = list(col("lessons").find(
        {"kind": {"$in": list(STEP_KINDS)}, "$or": [{"system": a, "op": b} for a, b in sorted(pairs)]},
    ).sort([("last_seen", DESCENDING)]))
    ver = _versions(version_fn)
    applied, lines = [], []
    for ls in found:
        cur = ver(ls["system"])
        if ls.get("stale") or cur is None or ls.get("system_version") != cur:
            lines.append(f"Lesson {ls['_id']} ({ls['op']} {ls['kind']}) was learned on {ls['system']} "
                         f"v{ls.get('system_version')}; {ls['system']} is v{cur} now — stale, not applying it.")
            continue
        tag = {"id": ls["_id"], "kind": ls["kind"], "why": ls.get("why"), "system_version": ls["system_version"],
               "incident": ls.get("last_incident"), "count": ls.get("count", 1)}
        for s in plan_steps:
            if (s["system"], s["op"]) == (ls["system"], ls["op"]):
                s.setdefault("lessons", []).append(tag)
                if ls["kind"] == "lied":
                    s["distrust_ack"] = True
        applied.append(ls)
        lines.append(narrate(ls))
    _used([ls["_id"] for ls in applied], incident)
    return applied, lines


def narrate(ls: dict) -> str:
    sys, op, v, inc = ls["system"], ls["op"], ls.get("system_version"), ls.get("last_incident")
    rec = f" ({ls['recovered_by']})" if ls.get("recovered_by") else ""
    if ls["kind"] == "lied":
        return (f"Lesson {ls['_id']}: last time {sys} v{v} ACKed {op} as applied but nothing changed ({inc}){rec}. "
                f"Not trusting its ACK: the Guard rejects a repeat lie on sight and the retry goes straight out.")
    if ls["kind"] == "not_applied":
        return (f"Lesson {ls['_id']}: last time {op} on {sys} v{v} was accepted but never landed ({inc}){rec}; "
                f"waiting for read-back, not the ACK.")
    if ls["kind"] == "refused":
        return f"Lesson {ls['_id']}: last time {sys} v{v} refused {op} ({inc}){rec}: {ls.get('why', '')[:80]}"
    return f"Lesson {ls['_id']}: last time {sys} v{v} never answered {op} ({inc}){rec}; read-back decides."


# ---------------------------------------------------------------------------------------------- read: recall
def skip_misfits(hits: list[dict], summary: list[dict], usable: dict | None, symptom_text: str,
                 incident: str) -> tuple[dict | None, list[str]]:
    """Wrong-hypothesis memory: a recalled playbook that didn't fit these exact symptoms before is skipped
    (marked unusable in the recall summary) and the next usable hit, if any, is taken instead."""
    if not hits:
        return usable, []
    ids = [h["_id"] for h in hits]
    bad = {ls["playbook"]: ls for ls in col("lessons").find(
        {"kind": "misfit", "stale": False, "playbook": {"$in": ids}, "symptom_text": symptom_text})}
    if not bad:
        return usable, []
    lines = []
    for s in summary:
        ls = bad.get(s["playbook_id"])
        if ls and s.get("usable"):
            s["usable"], s["skipped_by"] = False, ls["_id"]
            lines.append(f"Skipping {s['playbook_id']} (score {s['score']:.2f}): it didn't fit these symptoms "
                         f"before — {ls.get('why')} ({ls.get('last_incident')}, {ls['_id']}).")
    pick = next((s["playbook_id"] for s in summary if s.get("usable")), None)
    new = next((h for h in hits if h["_id"] == pick), None)
    _used([ls["_id"] for ls in bad.values()], incident)
    return new, lines


def stale_note(symptom_text: str) -> str | None:
    """Visible staleness: playbooks for these exact symptoms that a version bump made stale."""
    rows = list(col("playbooks").find({"symptom_text": symptom_text, "stale": True},
                                      {"_id": 1, "system": 1, "system_version": 1, "stale_reason": 1}).limit(3))
    if not rows:
        return None
    r = rows[0]
    return (f"{len(rows)} playbook(s) for these symptoms are stale ({', '.join(x['_id'] for x in rows)}: learned on "
            f"{r['system']} v{r.get('system_version')}, {r.get('stale_reason') or 'system changed'}).")


# ---------------------------------------------------------------------------------------------- read: viz/API
def items(current_versions: dict[str, int] | None = None, limit: int = 100) -> list[dict]:
    """The hive's memory, one flat list for the war room: playbooks, lessons and schema mappings, each with
    kind, system, version learned on, current version, stale, uses, last used."""
    cv = current_versions or {}
    out = []
    for p in col("playbooks").find({}, {"embedding": 0}).sort("created_at", DESCENDING).limit(limit):
        out.append({"kind": "playbook", "id": p["_id"], "system": p.get("system"),
                    "system_version": p.get("system_version"), "current_version": cv.get(p.get("system")),
                    "stale": bool(p.get("stale")), "stale_reason": p.get("stale_reason"),
                    "uses": p.get("uses", 0), "last_used_at": p.get("last_used_at"), "learned_at": p.get("created_at"),
                    "summary": f"{p.get('fault_class')}: {p.get('root_cause') or ''}"[:160],
                    "fix": [f.get("op") for f in p.get("fix") or []], "incident": p.get("incident"),
                    "embedding_model": p.get("embedding_model")})
    for ls in col("lessons").find().sort("last_seen", DESCENDING).limit(limit):
        out.append({"kind": "lesson", "lesson_kind": ls.get("kind"), "id": ls["_id"], "system": ls.get("system"),
                    "system_version": ls.get("system_version"), "current_version": cv.get(ls.get("system")),
                    "stale": bool(ls.get("stale")), "stale_reason": ls.get("stale_reason"),
                    "uses": ls.get("uses", 0), "last_used_at": ls.get("last_used_at"),
                    "learned_at": ls.get("first_seen"), "summary": ls.get("why"), "op": ls.get("op"),
                    "playbook": ls.get("playbook"), "count": ls.get("count", 1), "incidents": ls.get("incidents"),
                    "recovered_by": ls.get("recovered_by")})
    for m in col("schema_mappings").find().sort("ts", DESCENDING).limit(limit):  # W3's versioned mappings, if any
        sv = m.get("system_version")
        out.append({"kind": "schema_mapping", "id": m["_id"], "system": m.get("system"),
                    "system_version": sv, "current_version": cv.get(m.get("system")),
                    "mapping_version": m.get("version"),
                    "stale": bool(m.get("stale")) or (sv is not None and cv.get(m.get("system")) not in (None, sv)),
                    "stale_reason": m.get("stale_reason"), "uses": m.get("uses"), "last_used_at": m.get("ts"),
                    "learned_at": (m.get("history") or [{}])[0].get("ts") or m.get("ts"),
                    "summary": (f"{m.get('op')} needs {' → '.join(m.get('requires') or [])} first (workflow)"
                                if m.get("kind") == "workflow" else f"{m.get('field')} ← {m.get('source')} (was {m.get('previous')})"),
                    "incident": m.get("learned_from")})
    return out


INDEXES = [
    ([("system", ASCENDING), ("op", ASCENDING), ("stale", ASCENDING), ("system_version", ASCENDING)], {}),
    ([("kind", ASCENDING), ("playbook", ASCENDING), ("symptom_text", ASCENDING)], {}),
    ([("incidents", ASCENDING)], {}),
    ([("last_seen", DESCENDING)], {}),
]

LESSON_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "kind", "system", "system_version", "why", "stale", "first_seen"],
        "properties": {
            "kind": {"enum": list(KINDS)},
            "stale": {"bsonType": "bool"},
            "why": {"bsonType": "string", "minLength": 1},
        },
    }
}


def problem_signature(incident: dict) -> str:
    """Stable recurrence identity independent of incident ids and retry counters."""
    import hashlib
    import json
    system = incident.get('root_cause_system') or incident.get('system')
    fault = incident.get('fault_class')
    symptoms = sorted(re.sub(r'\b\d+(?:\.\d+)?\b', '#', s.lower())
                      for s in incident.get('symptoms') or [])
    identity = [system, fault if fault not in (None, 'unknown') else symptoms]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
