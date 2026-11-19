"""Schema drift: notice when a legacy feed renames a field, and learn the mapping (all in MongoDB).

The Hive knows one feed today: the unix xfer manifest the portal unmarshals (``GET /unix/state`` ->
``feed.sample``, a few records like ``{acct, balance, as_of}``).

* ``swarm.schema_samples``: the sentinel keeps one known-good sample per feed (``_id: "good:unix:xfer_manifest"``)
  while the feed matches the expected schema; diagnosis inserts every observed sample (TTL 24 h).
* Drift check = MongoDB, not Python: the observed sample is matched against the expected schema with a
  ``$jsonSchema`` query, then an aggregation (``$objectToArray`` + ``$setDifference`` + ``$type``) diffs its
  fields against the good sample. A missing field + a new field of a compatible type whose values line up
  account by account with the good sample = a proposed mapping.
* ``swarm.schema_mappings``: one doc per logical field (``_id: "unix.xfer_manifest.balance"``) with ``source``
  (the manifest field to read), ``version`` and ``history``. Written only after the Guard verified the
  mapping by read-back. Every agent reads expectations from here, so the next drift is diffed against the
  learned schema (v2 -> v3), not the original one. A round reset (warroom ``/api/reset`` next_round) puts the live mapping back to v1
  (the estate is v1 again) but keeps ``history``, so the same vendor patch next night is "seen before" and the
  verified version is reused. Only a full reset deletes the collection.

Who proposes the mapping (renaming is a meaning problem, so the AI gets a say):
  memory  "seen before": schema_mappings.history already holds a Guard-verified mapping for this exact change
          (or a non-stale schema_drift playbook maps to one of the new fields) -> reuse it, no Claude call. It
          gets the same value check as Claude's answer; if its values no longer line up it is marked stale and
          Claude / the rules propose instead
  claude  with a key and AI_SECOND_OPINION=1: Claude names the new field that means the same thing; the rules
          pre-check it (field exists, numeric, values line up) and overrule it if it doesn't fit
  rules   otherwise (no key, flag off, timeout, error, overruled): best type + value line-up match
Whatever proposes, only the Guard's read-back turns it into a stored mapping. With the flag on, the provenance
is written to ``incident.ai`` (``kind:"schema_mapping"``, the shared shape in CONTRACTS §8); no decision reads it.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError

from common import config, llm
from common.db import col, now

SYSTEM, FEED, CONSUMER = "unix", "xfer_manifest", "portal"
KEY = "acct"                                    # record key used to line values up
LOGICAL = {"acct": "string", "balance": "number", "as_of": "string"}   # v1 contract
NUMERIC = {"int", "long", "double", "decimal"}
# Values "line up" when every shared account is within 35% of the good sample: same units and magnitude
# (catches cents-vs-dollars or a count field), while tolerating the NIGHTBAT postings that move balances
# by a few percent between the good sample (refreshed every 10 s) and the verification.
TOLERANCE = 0.35


AI_TIMEOUT_S = float(os.environ.get("SCHEMA_AI_TIMEOUT_S", 6))
GUARD_RULE = "Guard read-back (portal reads the field, values line up)"
PRECHECK_RULE = "R1 field exists + numeric + values line up"


def ai_enabled() -> bool:
    return llm.llm_available() and os.environ.get("AI_SECOND_OPINION", "").lower() in ("1", "true", "on", "yes")


def _mid(field: str, system: str = SYSTEM, feed: str = FEED) -> str:
    return f"{system}.{feed}.{field}"


def mapping(field: str = "balance", system: str = SYSTEM, feed: str = FEED) -> dict:
    """Current mapping for a logical field (v1 identity if nothing was learned yet)."""
    doc = col("schema_mappings").find_one({"_id": _mid(field, system, feed)})
    return doc or {"_id": _mid(field, system, feed), "system": system, "feed": feed, "field": field,
                   "source": field, "version": 1}


def expected_fields(system: str = SYSTEM, feed: str = FEED) -> dict[str, str]:
    """Physical field name -> logical type, as the Hive currently expects the feed to look."""
    out = {}
    for logical, typ in LOGICAL.items():
        src = mapping(logical, system, feed)["source"] if logical == "balance" else logical
        out[src] = typ
    return out


def json_schema(system: str = SYSTEM, feed: str = FEED) -> dict:
    """$jsonSchema for a sample document whose ``record`` must have the expected fields."""
    props = {k: ({"bsonType": ["int", "long", "double", "decimal"]} if t == "number" else {"bsonType": "string"})
             for k, t in expected_fields(system, feed).items()}
    return {"bsonType": "object", "required": ["record"],
            "properties": {"record": {"bsonType": "object", "required": sorted(props), "properties": props}}}


def _sample(state: dict) -> list[dict]:
    return [r for r in ((state or {}).get("feed") or {}).get("sample") or [] if isinstance(r, dict)]


def conforms(sample_id) -> bool:
    return col("schema_samples").count_documents({"_id": sample_id, "$jsonSchema": json_schema()}, limit=1) == 1


def capture_good(state: dict) -> bool:
    """Sentinel: keep the latest sample that matches the expected schema as the feed's known-good sample."""
    recs = _sample(state)
    if not recs:
        return False
    gid = f"good:{SYSTEM}:{FEED}"
    doc = {"system": SYSTEM, "feed": FEED, "record": recs[0], "records": recs, "good": True,
           "schema_version": (state.get("feed") or {}).get("schema_version"), "ts": now()}
    tmp = col("schema_samples").insert_one({**doc, "good": None, "kind": "probe"}).inserted_id
    try:
        if not conforms(tmp):
            return False
        col("schema_samples").replace_one({"_id": gid}, doc, upsert=True)
        return True
    finally:
        col("schema_samples").delete_one({"_id": tmp})


def good_sample() -> dict | None:
    return col("schema_samples").find_one({"_id": f"good:{SYSTEM}:{FEED}"})


def lined_up(ok: int, total: int) -> bool:
    """Most accounts line up. Not all: a batch re-run between the good sample and the drift legitimately moves
    some balances past TOLERANCE (a drift landing mid batch-repair), while a wrong field (other units) misses all."""
    return total > 0 and ok * 2 > total


def line_up(good_records: list[dict], old: str, new_records: list[dict], new: str) -> tuple[int, int]:
    """How many accounts present in both samples have new[new] ≈ good[old] (within TOLERANCE)."""
    good = {r.get(KEY): r.get(old) for r in good_records or []}
    ok = total = 0
    for r in new_records or []:
        a, v = r.get(KEY), r.get(new)
        g = good.get(a)
        if a is None or not isinstance(g, (int, float)):
            continue
        total += 1
        if isinstance(v, (int, float)) and abs(v - g) <= TOLERANCE * max(abs(g), 1):
            ok += 1
    return ok, total


FIELD_DIFF = lambda sid, gid: [  # noqa: E731 — the aggregation that diffs the two samples' fields
    {"$match": {"_id": sid}},
    {"$lookup": {"from": "schema_samples", "pipeline": [{"$match": {"_id": gid}}], "as": "good"}},
    {"$set": {"good": {"$first": "$good"}}},
    {"$project": {
        "new": {"$map": {"input": {"$objectToArray": "$record"}, "in": {"k": "$$this.k", "t": {"$type": "$$this.v"}}}},
        "old": {"$map": {"input": {"$objectToArray": {"$ifNull": ["$good.record", {}]}},
                         "in": {"k": "$$this.k", "t": {"$type": "$$this.v"}}}}}},
    {"$project": {"new": 1, "old": 1,
                  "missing": {"$setDifference": ["$old.k", "$new.k"]},
                  "added": {"$setDifference": ["$new.k", "$old.k"]}}},
]


def detect(state: dict) -> dict | None:
    """Diagnosis on unix: None if the feed matches what the Hive expects, else a schema_drift finding
    ``{fault_class, root_cause, facts, params}`` (params carry the proposed mapping)."""
    recs = _sample(state)
    if not recs:
        return None
    sid = col("schema_samples").insert_one({
        "system": SYSTEM, "feed": FEED, "record": recs[0], "records": recs, "good": None, "kind": "observed",
        "schema_version": (state.get("feed") or {}).get("schema_version"), "ts": now()}).inserted_id
    if conforms(sid):
        col("schema_samples").update_one({"_id": sid}, {"$set": {"good": True}})
        return None
    col("schema_samples").update_one({"_id": sid}, {"$set": {"good": False}})
    expected = expected_fields()
    good = good_sample()
    if good:
        diff = next(col("schema_samples").aggregate(FIELD_DIFF(sid, good["_id"])), None) or {}
        missing, added = diff.get("missing") or [], diff.get("added") or []
        types = {f["k"]: f["t"] for f in diff.get("new") or []}
    else:  # no good sample yet: diff against the expected schema
        missing = [k for k in expected if k not in recs[0]]
        added = [k for k in recs[0] if k not in expected]
        types = {k: ("double" if isinstance(v, float) else "int" if isinstance(v, int) else "string")
                 for k, v in recs[0].items()}
    cur = mapping("balance")
    old = cur["source"]
    facts = [f"$jsonSchema v{cur['version']} mismatch: missing {missing}, added {added}"]
    if old not in missing:
        return {"fault_class": "schema_drift", "root_cause": f"{FEED} schema changed: missing {missing}, added {added}",
                "facts": facts, "params": {}}
    def score(new: str) -> tuple[bool, int, int]:
        ok, total = line_up(good["records"], old, recs, new) if good else (0, 0)
        return (types.get(new) in NUMERIC and (lined_up(ok, total) or not good)), ok, total

    cands = sorted(((*score(n), n) for n in added if types.get(n) in NUMERIC), reverse=True)
    if not cands:
        return {"fault_class": "schema_drift", "root_cause": f"{FEED} dropped field {old}; no numeric replacement",
                "facts": facts, "params": {}}
    new, how, ai = cands[0][3], "rules", None
    mem = remembered(old, added)
    if mem:  # MongoDB already knows this rename: same value check as Claude's answer, no Claude call
        fits, ok, total = score(mem["source"]) if mem["source"] in added else (False, 0, 0)
        if not fits:
            forget(mem, f"{mem['source']} no longer lines up with {old} ({ok}/{total})")
            facts.insert(0, f"memory: {mem['label']} said {old} → {mem['source']}, but its values don't line up "
                            f"({ok}/{total}): marked stale")
            mem = None
    if mem:
        new, how = mem["source"], "memory"
        facts.insert(0, f"seen before: {mem['label']} reused ({old} → {new}, verified on {mem['incident']}; "
                        f"values line up {ok}/{total}; no Claude call)")
        params_extra = {"reused_version": mem.get("version"), "memory": mem["label"]}
        if ai_enabled():
            ai = _ai_doc(None, f"Recalled from {mem['label']}: {old} was renamed {new} before.",
                         [mem["cite"]], 1.0, 0, old, new, "memory", "accepted", "memory: verified before")
    else:
        params_extra = {}
    if not mem and ai_enabled():
        prop = ask_claude(old, missing, added, good["record"] if good else None, recs[0])
        if prop.get("source"):
            fits, ok, total = score(prop["source"]) if prop["source"] in added else (False, 0, 0)
            if fits:
                new, how = prop["source"], "claude"
                facts.insert(0, f"Claude proposed {old} → {new} ({prop.get('confidence')}, {prop['latency_ms']} ms)")
                verdict, reason = "accepted", f"{new} is numeric and lines up {ok}/{total} with the good sample"
            else:
                facts.insert(0, f"Claude proposed {old} → {prop['source']}; rules overruled it, using {new}")
                verdict, reason = "overruled", (f"{prop['source']} is not a new numeric field whose values line up; "
                                                f"rules picked {new}")
            ai = _ai_doc(prop.get("model"), prop.get("hypothesis") or "", prop.get("cites") or [],
                         prop.get("confidence"), prop["latency_ms"], old, prop["source"], "claude", verdict, reason)
        else:
            facts.insert(0, f"Claude unavailable ({prop.get('error')}); rules matched {new}")
            ai = {**_ai_doc(prop.get("model"), "", [], None, prop["latency_ms"], old, new, "rules", "accepted",
                            "fallback: type + value line-up"), "status": "offline", "error": prop.get("error")}
    lined, ok, total = score(new)
    if how == "rules":
        facts.insert(0, f"rules: no playbook/op for unknown field {new}; type + value match {old} → {new}")
    facts.append(f"{new} is {types.get(new)} like {old}, values line up {ok}/{total}"
                 if good else f"{new} is {types.get(new)} like {old} (no good sample)")
    params = {"field": "balance", "from": old, "to": new, "sample_id": str(sid), "lined_up": [ok, total],
              "version": next_version(old, new), "proposed_by": how, **params_extra}
    if ai:
        params["ai"] = ai
    return {"fault_class": "schema_drift",
            "root_cause": f"unix {FEED} renamed {old} → {new} (vendor patch); portal still reads {old}",
            "facts": facts, "params": params}


def remembered(old: str, added: list[str]) -> dict | None:
    """Memory for this change: a Guard-verified history entry ``old -> one of added`` in schema_mappings (kept
    across round resets), else a non-stale schema_drift playbook whose fix maps to one of today's new fields."""
    doc = col("schema_mappings").find_one({"_id": _mid("balance")}, {"history": 1}) or {}
    for e in reversed(doc.get("history") or []):
        if e.get("from") == old and e.get("to") in added and not e.get("stale"):
            return {"kind": "mapping", "source": e["to"], "version": e.get("version"), "incident": e.get("incident"),
                    "label": f"mapping v{e.get('version')}", "cite": f"schema_mappings v{e.get('version')}"}
    pb = col("playbooks").find_one({"fault_class": "schema_drift", "stale": False,
                                    "fix": {"$elemMatch": {"op": "map_field", "params.source": {"$in": added}}}},
                                   {"fix": 1, "incident": 1}, sort=[("created_at", DESCENDING)])
    if not pb:
        return None
    src = next(f["params"]["source"] for f in pb["fix"] if f.get("op") == "map_field"
               and (f.get("params") or {}).get("source") in added)
    return {"kind": "playbook", "source": src, "version": None, "incident": pb.get("incident") or pb["_id"],
            "label": f"playbook {pb['_id']}", "cite": pb["_id"], "playbook_id": pb["_id"]}


def forget(mem: dict, reason: str) -> None:
    """A remembered mapping failed today's value check: mark it stale so nobody reuses it blindly."""
    if mem["kind"] == "mapping":
        col("schema_mappings").update_one(
            {"_id": _mid("balance")}, {"$set": {"history.$[e].stale": True, "history.$[e].stale_reason": reason}},
            array_filters=[{"e.to": mem["source"], "e.version": mem["version"]}])
    else:
        col("playbooks").update_one({"_id": mem["playbook_id"]}, {"$set": {
            "stale": True, "staled_at": now(), "stale_reason": reason}})


def _verified_before(doc: dict, old: str, new: str) -> dict | None:
    return next((e for e in reversed(doc.get("history") or [])
                 if e.get("from") == old and e.get("to") == new and not e.get("stale")), None)


def next_version(old: str, new: str, field: str = "balance") -> int:
    """The version a verified old -> new mapping gets: its earlier version if this exact change was verified
    before (reused), else one past every version ever stored."""
    doc = col("schema_mappings").find_one({"_id": _mid(field)}) or {}
    seen = _verified_before(doc, old, new)
    if seen:
        return int(seen["version"])
    return max([int(doc.get("version") or 1)] + [int(e.get("version") or 1) for e in doc.get("history") or []]) + 1


def _ai_doc(model, hypothesis, cites, confidence, latency_ms, old, source, how, verdict, reason) -> dict:
    op = {"op": "map_field", "target": CONSUMER, "params": {"field": "balance", "source": source}, "risk": "safe",
          "why": f"portal still reads {old}; the manifest now sends {source}"}
    return {"kind": "schema_mapping", "status": "done", "model": model, "hypothesis": hypothesis, "cites": cites,
            "confidence": confidence, "latency_ms": latency_ms, "proposed_by": how, "proposed_ops": [op],
            "verdicts": [{"op": "map_field", "target": CONSUMER, "verdict": verdict, "rule": PRECHECK_RULE,
                          "reason": reason}],
            "guard": {"verdict": "pending"}, "ts": now()}


SYSTEM_PROMPT = ("You map renamed fields in a legacy bank's data feed. A vendor patch renamed one field. "
                 "Answer with one JSON object only, no prose.")


def ask_claude(old: str, missing: list[str], added: list[str], good_rec: dict | None, new_rec: dict) -> dict:
    """Ask Claude which new field carries the meaning of `old`. Hard time budget, never raises.
    Returns {source?, hypothesis, cites, confidence, model, latency_ms, error?}."""
    prompt = (f"The consumer reads field `{old}` (the account balance in cents).\n"
              f"Old record (before the patch): {json.dumps(good_rec, default=str) if good_rec else 'unknown'}\n"
              f"New record (after the patch): {json.dumps(new_rec, default=str)}\n"
              f"Fields missing now: {missing}. Fields that appeared: {added}.\n"
              'Reply as {"source": "<one of the fields that appeared, carrying the same meaning as '
              f'{old}>", "hypothesis": "<one sentence>", "confidence": <0..1>, '
              '"cites": ["old.<field>", "new.<field>"]}')
    box: dict = {}
    t0 = time.monotonic()
    th = threading.Thread(target=lambda: box.update(text=llm.complete(SYSTEM_PROMPT, prompt, max_tokens=200)),
                          daemon=True)
    th.start()
    th.join(AI_TIMEOUT_S)
    out = {"model": config.LLM_MODEL, "latency_ms": int((time.monotonic() - t0) * 1000), "hypothesis": "",
           "cites": [], "confidence": None}
    if th.is_alive():
        return {**out, "error": f"timeout after {AI_TIMEOUT_S:g}s"}
    text = box.get("text")
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {**out, "error": "no answer" if not text else "no JSON in answer"}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {**out, "error": "bad JSON"}
    src = str(data.get("source") or "").strip()
    conf = data.get("confidence")
    return {**out, "source": src or None, "hypothesis": str(data.get("hypothesis") or "")[:300],
            "cites": [str(c)[:60] for c in (data.get("cites") or [])][:6],
            "confidence": round(float(conf), 2) if isinstance(conf, (int, float)) else None,
            **({} if src else {"error": "no source in answer"})}


def record_ai(incident: str, ai: dict) -> None:
    """Advisory provenance on the incident (shared `ai` shape); never read by a decision."""
    col("incidents").update_one({"_id": incident}, {"$set": {"ai": ai}})


def guard_verdict(incident: str, source: str, evidence: str, version: int | None) -> dict | None:
    """Stamp the Guard's read-back onto incident.ai (only our own kind) and return the updated doc."""
    from pymongo import ReturnDocument  # noqa: PLC0415
    lined = re.search(r"(\d+)/(\d+) accounts", evidence or "")
    reason = (f"{lined.group(1)}/{lined.group(2)} values match the good sample" if lined else "read-back passed") + \
             (f"; mapping v{version} stored" if version else "")
    return col("incidents").find_one_and_update(
        {"_id": incident, "ai.kind": "schema_mapping"},
        {"$set": {"ai.guard": {"verdict": "verified", "rule": GUARD_RULE, "reason": reason, "source": source,
                               "version": version, "ts": now()}}},
        return_document=ReturnDocument.AFTER, projection={"ai": 1})


def verify_mapping(field: str, source: str, portal_state: dict, unix_state: dict) -> tuple[bool, str]:
    """Guard read-back for map_field: the portal reads `source`, the manifest carries it, and its values line
    up with the last good sample."""
    pm = ((portal_state.get("feed") or {}).get("map") or {}).get(field)
    recs = _sample(unix_state)
    present = sum(1 for r in recs if isinstance(r.get(source), (int, float)))
    good = good_sample()
    old = mapping(field)["source"]
    if good:
        ok, total = line_up(good["records"], old, recs, source)
        lined = f"values line up with good sample: {ok}/{total} accounts ({old} → {source})"
        values_ok = lined_up(ok, total)
    else:
        lined, values_ok = "no good sample: numeric check only", present > 0
    applied = pm == source and recs and present == len(recs) and values_ok
    return bool(applied), (f"state.portal.feed.map.{field}={pm}; unix manifest has numeric {source} in "
                           f"{present}/{len(recs)} records; {lined}")


def learn(field: str, source: str, incident: str, step_id: str, evidence: str, agent: str) -> dict | None:
    """Store the verified mapping. A change verified before keeps its version (``reused``), a new one gets
    max version + 1. Idempotent: returns None if it is already the live mapping."""
    cur = mapping(field)
    if cur["source"] == source:
        return None
    ver = next_version(cur["source"], source, field)
    reused = bool(_verified_before(col("schema_mappings").find_one({"_id": cur["_id"]}) or {}, cur["source"], source))
    entry = {"version": ver, "from": cur["source"], "to": source, "incident": incident, "step_id": step_id,
             "verified_by": agent, "evidence": evidence, "reused": reused, "ts": now()}
    try:
        col("schema_mappings").update_one(
            {"_id": cur["_id"], "source": {"$ne": source}},
            {"$set": {"system": SYSTEM, "feed": FEED, "field": field, "source": source, "previous": cur["source"],
                      "version": ver, "reused": reused, "consumer": CONSUMER, "learned_from": incident,
                      "verified_by": agent, "evidence": evidence, "ts": now()},
             "$push": {"history": entry}},
            upsert=True)
    except DuplicateKeyError:
        return None  # someone already stored this exact mapping
    return {**entry, "_id": cur["_id"]}


def learn_verified(field: str, source: str, incident: str, step_id: str, evidence: str, agent: str,
                   unix_state: dict | None) -> dict | None:
    """Guard, after map_field is verified: store the mapping, then re-capture the known-good sample right away
    (the manifest already conforms to the new expectation). Without this the good sample keeps the old field
    name for up to SAMPLE_EVERY_S, and a second drift in that window can't be diffed or verified."""
    m = learn(field, source, incident, step_id, evidence, agent)
    if unix_state is not None:
        capture_good(unix_state)
    return m


def feed_change(state: dict) -> tuple[list[str], list[str]] | None:
    """Sentinel: (missing, added) fields of the current manifest vs. what the Hive expects; None if it conforms."""
    recs = _sample(state)
    if not recs:
        return None
    exp = expected_fields()
    missing = sorted(k for k in exp if k not in recs[0])
    added = sorted(k for k in recs[0] if k not in exp)
    return (missing, added) if missing or added else None


def latest_mappings(limit: int = 10) -> list[dict]:
    return list(col("schema_mappings").find().sort("ts", DESCENDING).limit(limit))
