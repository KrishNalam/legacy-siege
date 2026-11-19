"""Playbook memory ("honey"): Atlas Vector Search recall and playbook writing."""
from __future__ import annotations

from common import config, embed
from common.db import col, next_id, now

NUM_CANDIDATES = 50
LIMIT = 3


def _cross_space(symptom_text: str, model: str) -> list[dict]:
    """Playbooks embedded by a DIFFERENT model than the query (a Voyage call fell back to local-hash, or was
    rate-limited when the playbook was written). The vector index can't compare across spaces, so score those
    by local-hash cosine of the stored symptom_text vs the query text. Tiny scan (non-stale playbooks only)."""
    q = embed._local(symptom_text)
    out = []
    for pb in col("playbooks").find({"stale": False, "embedding_model": {"$ne": model}}, {"embedding": 0}).limit(200):
        if not pb.get("symptom_text"):
            continue
        v = embed._local(pb["symptom_text"])
        pb["score"] = sum(a * b for a, b in zip(q, v))
        pb["via"] = "text-match"
        out.append(pb)
    return sorted(out, key=lambda h: -h["score"])[:LIMIT]


def search(symptom_text: str) -> tuple[dict, list[dict]]:
    """$vectorSearch over swarm.playbooks by symptom text. Returns (filter, hits-with-scores).
    We pre-filter on the embedding model (vector spaces never mix) and staleness; we do NOT filter on
    the root system because it is unknown until diagnosis."""
    vector, model = embed.embed_with_model(symptom_text, "query")
    flt = {"embedding_model": model, "stale": False}
    pipeline = [
        {"$vectorSearch": {"index": "playbook_vec", "path": "embedding",
                           "queryVector": vector,
                           "numCandidates": NUM_CANDIDATES, "limit": LIMIT, "filter": flt}},
        {"$project": {"embedding": 0}},
        {"$addFields": {"score": {"$meta": "vectorSearchScore"}}},
    ]
    hits = list(col("playbooks").aggregate(pipeline))
    if not any(h["score"] >= config.RECALL_THRESHOLD for h in hits):
        # Mixed vector spaces (partial Voyage outage / rate limit): never lose the round-2 speedup for it.
        hits = sorted(hits + _cross_space(symptom_text, model), key=lambda h: -h["score"])[:LIMIT]
    return flt, hits


def evaluate(hits: list[dict], current_version) -> tuple[dict | None, list[dict]]:
    """Pick the best usable hit: score >= RECALL_THRESHOLD and learned on the root system's CURRENT version.
    `current_version(sys)` reads the live version over HTTP. Returns (usable_hit|None, hit summaries)."""
    usable, summary, versions = None, [], {}
    for h in hits:
        sys = h.get("system")
        if sys not in versions:
            try:
                versions[sys] = current_version(sys)
            except Exception:
                versions[sys] = None
        ok_version = versions[sys] is not None and h.get("system_version") == versions[sys]
        ok = h["score"] >= config.RECALL_THRESHOLD and ok_version and not h.get("stale")
        summary.append({"playbook_id": h["_id"], "score": round(float(h["score"]), 4), "stale": bool(h.get("stale")),
                        "fault_class": h.get("fault_class"), "system": sys, "system_version": h.get("system_version"),
                        "current_version": versions[sys], "usable": ok,
                        **({"via": h["via"]} if h.get("via") else {})})
        if ok and usable is None:
            usable = h
    return usable, summary


def build_playbook(inc: dict, symptom_text: str, root_version: int) -> dict:
    """Every resolved incident becomes a playbook (embedded so the next incident can recall it).
    Built (and embedded) outside the resolve transaction; inserted inside it."""
    steps = [s for s in inc.get("steps") or [] if not s.get("superseded_by_replan")]
    vector, model = embed.embed_with_model(symptom_text, "document")
    return {
        "_id": next_id("PB"),
        "incident": inc["_id"], "system": inc.get("root_cause_system"),
        "symptom_system": inc.get("system"), "system_version": root_version,
        "fault_class": inc.get("fault_class"), "symptoms": inc.get("symptoms", []),
        "symptom_text": symptom_text, "root_cause": inc.get("root_cause"),
        "fix": [{"system": s["system"], "op": s["op"], "params": s.get("params", {}), "desc": s.get("desc"),
                 "destructive": bool(s.get("destructive"))} for s in steps],
        "outcome": "resolved", "metrics": inc.get("metrics", {}),
        "embedding": vector, "embedding_model": model,
        "stale": False, "uses": 0, "created_at": now(), "verified_at": now(),
    }
