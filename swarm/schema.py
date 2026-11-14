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
  learned schema (v2 -> v3), not the original one.
"""
from __future__ import annotations

from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError

from common.db import col, now

SYSTEM, FEED, CONSUMER = "unix", "xfer_manifest", "portal"
KEY = "acct"                                    # record key used to line values up
LOGICAL = {"acct": "string", "balance": "number", "as_of": "string"}   # v1 contract
NUMERIC = {"int", "long", "double", "decimal"}
# Values "line up" when every shared account is within 35% of the good sample: same units and magnitude
# (catches cents-vs-dollars or a count field), while tolerating the NIGHTBAT postings that move balances
# by a few percent between the good sample (refreshed every 10 s) and the verification.
TOLERANCE = 0.35


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
    facts = [f"$jsonSchema(expected v{cur['version']}: {', '.join(sorted(expected))}) does not match the manifest",
             f"missing={missing} added={added}"]
    if old not in missing:
        return {"fault_class": "schema_drift", "root_cause": f"{FEED} schema changed: missing {missing}, added {added}",
                "facts": facts, "params": {}}
    cands = []
    for new in added:
        if types.get(new) not in NUMERIC:
            continue
        ok, total = line_up(good["records"], old, recs, new) if good else (0, 0)
        cands.append((ok == total and total > 0, ok, total, new))
    cands.sort(reverse=True)
    if not cands:
        return {"fault_class": "schema_drift", "root_cause": f"{FEED} dropped field {old}; no numeric replacement",
                "facts": facts, "params": {}}
    lined, ok, total, new = cands[0]
    facts.append(f"{new} is {types.get(new)} like {old}; values line up with the good sample for {ok}/{total} accounts"
                 if good else f"{new} is {types.get(new)} like {old} (no good sample to line values up)")
    return {"fault_class": "schema_drift",
            "root_cause": f"unix {FEED} renamed {old} → {new} (vendor patch); portal still reads {old}",
            "facts": facts,
            "params": {"field": "balance", "from": old, "to": new, "sample_id": str(sid), "lined_up": [ok, total],
                       "version": cur["version"] + 1}}


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
        values_ok = total > 0 and ok == total
    else:
        lined, values_ok = "no good sample: numeric check only", present > 0
    applied = pm == source and recs and present == len(recs) and values_ok
    return bool(applied), (f"state.portal.feed.map.{field}={pm}; unix manifest has numeric {source} in "
                           f"{present}/{len(recs)} records; {lined}")


def learn(field: str, source: str, incident: str, step_id: str, evidence: str, agent: str) -> dict | None:
    """Store the verified mapping (version + 1). Idempotent: returns None if it was already learned."""
    cur = mapping(field)
    if cur["source"] == source:
        return None
    ver = int(cur.get("version") or 1) + 1
    entry = {"version": ver, "from": cur["source"], "to": source, "incident": incident, "step_id": step_id,
             "verified_by": agent, "evidence": evidence, "ts": now()}
    try:
        col("schema_mappings").update_one(
            {"_id": cur["_id"], "source": {"$ne": source}},
            {"$set": {"system": SYSTEM, "feed": FEED, "field": field, "source": source, "previous": cur["source"],
                      "version": ver, "consumer": CONSUMER, "learned_from": incident, "verified_by": agent,
                      "evidence": evidence, "ts": now()},
             "$push": {"history": entry}},
            upsert=True)
    except DuplicateKeyError:
        return None  # someone already stored this exact mapping
    return {**entry, "_id": cur["_id"]}


def latest_mappings(limit: int = 10) -> list[dict]:
    return list(col("schema_mappings").find().sort("ts", DESCENDING).limit(limit))
