"""AI second opinion: Claude proposes, verification disposes.

After the Scout writes its plan, `start()` spawns ONE background thread per incident. It numbers the evidence the
Hive gathered (symptoms, each hop's status/blames/facts, the last few log lines per system; never the policy's own
fault class, root cause or plan) and asks Claude for the fastest recovery it believes is safe: a hypothesis, the
evidence ids it relies on, a confidence and an op plan. A deterministic judge then checks every proposed op against
the plan the policy is actually running and against read-backs taken BEFORE the call, and names the rule that
decided: accepted, or overruled with a reason. The result lands on the incident as the optional, advisory `ai`
field (shared shape: CONTRACTS.md §8), plus a running tally in swarm.stats.

Nothing here is ever executed and no decision path reads `incidents.ai`. Flag off (AI_SECOND_OPINION), no key, at
capacity, on timeout or on any error → no `ai` (or `ai.status == "offline"`): the demo runs exactly as before.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from datetime import datetime

import pymongo
from pymongo import ReturnDocument

from common import config, events, llm
from common.db import col, now

from . import policy, settings
from .http import Systems

KIND = "second_opinion"
DEADLINE_S = 20.0          # end-to-end budget for read-backs + LLM + writes
LLM_TIMEOUT_S = 12.0
MAX_OPS = 8
LOG_LINES = 4              # last N /logs lines per system in the evidence
_slots = threading.BoundedSemaphore(2)  # at most two calls in flight per process; never queue

# Ops catalog (CONTRACTS §2): system -> op -> allowed param names, plus the only valid value of fixed params.
OPS = {
    "mainframe": {"quarantine_record": ["record_id"], "restart_batch": ["job"], "rotate_credential": ["account"]},
    "unix": {"purge_spool": ["path"], "restart_cron": ["job"], "chmod_config": ["file", "mode"], "rerun_transfer": []},
    "portal": {"flush_sessions": [], "apply_patch": ["patch"], "refresh_cache": [], "map_field": ["field", "source"]},
}
FIXED = {("restart_batch", "job"): "NIGHTBAT", ("rotate_credential", "account"): "OPERATOR",
         ("purge_spool", "path"): "/var/spool/xfer", ("restart_cron", "job"): "xfer_pull",
         ("chmod_config", "file"): "/etc/xfer.conf", ("chmod_config", "mode"): "0640",
         ("apply_patch", "patch"): "parameterize_search"}
DESTRUCTIVE_OPS = {op for _s, op in policy.DESTRUCTIVE}

# Named guardrail rules (shown on the card next to every verdict).
P1, P2, P3, P4, P5, P6 = ("P1 catalog", "P2 dependency order", "P3 blast radius", "P4 read-back", "P5 scope",
                          "P6 approval gate")
OK = "plan match"

# Every fix plan is a dependency chain (each op needs the previous one's effect). Specific reasons for the
# (op, prerequisite) pairs the plans contain; anything else gets a generic one.
PREREQ_WHY = {
    ("restart_batch", "quarantine_record"): "poison record still queued: NIGHTBAT would ABEND S0C7 again and re-post a half-run ledger",
    ("rerun_transfer", "restart_batch"): "no fresh batch file yet: NIGHTBAT hasn't re-run",
    ("rerun_transfer", "purge_spool"): "spool still full: the transfer would fail again",
    ("rerun_transfer", "restart_cron"): "xfer_pull still stopped: nothing would pull the file",
    ("refresh_cache", "rerun_transfer"): "would reload the same stale balances: the transfer hasn't re-run",
}

SYSTEM_PROMPT = (
    "You are the on-call SRE lead for a bank's legacy estate (mainframe -> unix middleware -> web portal; each "
    "system depends on the one before it). Customers are seeing wrong balances right now and leadership wants them "
    "fixed as fast as possible. Reason from the numbered evidence only: rank 2 root-cause hypotheses (most likely "
    "first, each with its supporting evidence ids and why), choose the root system, then give the FASTEST recovery "
    "plan you believe is safe, in order, using only ops from the catalog. Be very terse: hypothesis why under 14 "
    "words, op why under 8 words; at most 6 ops; confidence 0-1.")


def enabled() -> bool:
    """Background second opinion (advisory)."""
    return config.AI_SECOND_OPINION and llm.llm_available()


def drives() -> bool:
    """Claude's validated plan runs (synchronous, at plan time)."""
    return config.AI_DRIVES and llm.llm_available()


def owned_elsewhere(inc: dict) -> bool:
    """Another AI feature (e.g. W3's schema_mapping) already wrote incident.ai: leave it alone."""
    return (inc.get("ai") or {}).get("kind") not in (None, KIND)


def prewarm() -> None:
    if enabled() or drives():
        llm.prewarm()


def _schema() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["hypotheses", "root_system", "fault_class", "confidence", "proposed_ops"],
        "properties": {
            "hypotheses": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["system", "fault_class", "evidence", "why"],
                "properties": {"system": {"type": "string", "enum": list(config.SYSTEM_IDS)},
                               "fault_class": {"type": "string", "enum": sorted(policy.WEAKNESS) + ["other"]},
                               "evidence": {"type": "array", "items": {"type": "string"}},
                               "why": {"type": "string"}}}},
            "root_system": {"type": "string", "enum": list(config.SYSTEM_IDS)},
            "fault_class": {"type": "string", "enum": sorted(policy.WEAKNESS) + ["other"]},
            "confidence": {"type": "number"},
            "proposed_ops": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["target", "op", "params", "risk", "why"],
                "properties": {
                    "target": {"type": "string"}, "op": {"type": "string"}, "why": {"type": "string"},
                    "risk": {"type": "string", "enum": ["safe", "destructive"]},
                    "params": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False, "required": ["name", "value"],
                        "properties": {"name": {"type": "string"}, "value": {"type": "string"}}}},
                }}},
        },
    }


# ---------------------------------------------------------------------------------------------- evidence + prompt
def findings_of(inc: dict, state: dict) -> dict:
    """What the Hive collected, minus its own conclusions (no fault_class / root_cause / plan)."""
    findings = {}
    for sys, f in (state.get("findings") or {}).items():
        f = f or {}
        findings[sys] = {"status": f.get("status"), "blames": f.get("blames"), "facts": list(f.get("facts") or [])[:6]}
    return {"symptoms": list(inc.get("symptoms") or [])[:6], "path": list(state.get("path") or []),
            "findings": findings, "affected": sorted(inc.get("affected") or [], key=policy.rank)}


def number_evidence(ev: dict, logs: dict) -> list[dict]:
    """Flatten everything Claude may cite into numbered lines E1..En: {id, src, text}."""
    items: list[tuple[str, str]] = [("incident symptom", s) for s in ev["symptoms"]]
    for sys in ev["path"] or list(ev["findings"]):
        f = ev["findings"].get(sys)
        if f:
            items.append((f"{sys} hop", f"status={f['status']} blames={f['blames'] or 'nothing'}"))
            items += [(f"{sys} hop", str(x)) for x in f["facts"]]
    for sys in config.SYSTEM_IDS:
        for ln in (logs.get(sys) or [])[-LOG_LINES:]:
            items.append((f"{sys} /logs", f"{ln.get('level', '')} {ln.get('code', '')} {ln.get('line', '')}".strip()))
    # redact the FULL raw text first, then clean/truncate: this is both what Claude sees and what the card stores
    return [{"id": f"E{i}", "src": src, "text": llm.clean(llm.redact(text), 160)} for i, (src, text) in enumerate(items, 1)]


def build_prompt(ev: dict, evidence: list[dict], catalog: dict) -> str:
    lines = ["EVIDENCE"]
    if ev["affected"]:
        lines.append(f"(affected systems: {', '.join(ev['affected'])}; examined: {' -> '.join(ev['path']) or 'none'})")
    lines += [f"{e['id']} {llm.fence(e['src'], e['text'], 160)}" for e in evidence]  # system text = data
    lines.append("\nOPS CATALOG (target.op {params} — description)")
    for sys in config.SYSTEM_IDS:
        for o in catalog.get(sys) or []:
            op = llm.clean(o.get("op"), 40)
            p = llm.clean(", ".join(f"{k}={v}" for k, v in (o.get("params") or {}).items()), 80)
            lines.append(f"- {sys}.{op} {{{p}}} — {llm.fence(sys + ' ops', o.get('desc') or '', 90)}"
                         + (" [DESTRUCTIVE]" if o.get("destructive") else ""))
    return "\n".join(lines)


def static_catalog() -> dict:
    return {s: [{"op": op, "params": {k: FIXED.get((op, k), "<value>") for k in ks},
                 "destructive": op in DESTRUCTIVE_OPS, "desc": ""} for op, ks in ops.items()]
            for s, ops in OPS.items()}


# ---------------------------------------------------------------------------------------------- judge
def _norm(s) -> str:
    return str(s or "").strip().lower()


def normalize(out: dict, evidence_ids: set[str] | None = None) -> dict:
    """Validate and tidy Claude's JSON. Anything malformed becomes something the judge can overrule."""
    ops = []
    for p in (out.get("proposed_ops") or [])[:MAX_OPS]:
        if not isinstance(p, dict):
            continue
        target, op = _norm(p.get("target") or p.get("system")), _norm(p.get("op"))
        if "." in op:  # "unix.restart_cron"
            pre, _, op = op.partition(".")
            target = target or pre
        names = [_norm(x.get("name")) for x in p.get("params") or [] if isinstance(x, dict)]
        params = {_norm(x.get("name")): str(x.get("value") or "").strip()
                  for x in p.get("params") or [] if isinstance(x, dict) and _norm(x.get("name"))}
        ops.append({"target": target, "op": op, "params": params, "why": str(p.get("why") or "").strip()[:140],
                    "risk": "destructive" if _norm(p.get("risk")) == "destructive" else "safe",
                    "dup_params": len(names) != len(set(names))})
    try:
        conf = max(0.0, min(1.0, float(out.get("confidence"))))
    except (TypeError, ValueError):
        conf = None
    raw_cites = out.get("cites") or [c for h in out.get("hypotheses") or [] if isinstance(h, dict)
                                     for c in h.get("evidence") or []]
    cites = [str(c).strip().upper() for c in raw_cites if str(c).strip()]
    if evidence_ids is not None:  # a cite must point at a line we actually showed
        cites = [c for c in cites if c in evidence_ids]
    hyps = []
    for h in (out.get("hypotheses") or [])[:3]:
        if isinstance(h, dict):
            ev = [str(c).strip().upper() for c in h.get("evidence") or []]
            hyps.append({"system": _norm(h.get("system")), "fault_class": _norm(h.get("fault_class")),
                         "evidence": [c for c in ev if evidence_ids is None or c in evidence_ids][:5],
                         "why": str(h.get("why") or "").strip()[:160]})
    lead = str(out.get("hypothesis") or "").strip() or (
        f"{hyps[0]['system']}/{hyps[0]['fault_class']}: {hyps[0]['why']}" if hyps else "")
    return {"hypotheses": hyps, "root_system": _norm(out.get("root_system")), "fault_class": _norm(out.get("fault_class")),
            "hypothesis": lead[:300], "confidence": conf,
            "cites": list(dict.fromkeys(cites))[:8], "ops": ops}


def _same(op: str, k: str, a, b) -> bool:
    """Exact match (after trimming): the systems reject `nightbat` or `/VAR/SPOOL/XFER`."""
    return str(a).strip() == str(b).strip()


def _outside(p: dict, rb: dict) -> tuple[str, str]:
    """(rule, reason) for an op that isn't in the plan — from read-backs taken before Claude answered."""
    op = p["op"]
    unix, portal, mf = rb.get("unix"), rb.get("portal"), rb.get("mainframe")
    if op == "restart_cron" and unix and (unix.get("cron") or {}).get("xfer_pull") == "running":
        return P4, "xfer_pull is already running; a restart would interrupt a live transfer"
    if op == "flush_sessions" and portal and portal.get("sessions") is not None \
            and portal["sessions"] < settings.SESSION_HIGH:
        return P3, (f"{portal['sessions']} sessions is normal (limit {settings.SESSION_HIGH}); would log out every "
                    f"signed-in customer for nothing")
    if op == "purge_spool" and unix and ((unix.get("spool") or {}).get("disk_pct") or 0) < settings.DISK_HIGH:
        return P3, f"spool at {unix['spool']['disk_pct']}%; a purge would delete in-flight transfer files"
    if op == "restart_batch":
        st = ((mf or {}).get("batch") or {}).get("status")
        return P3, f"destructive core-ledger restart with no batch fault in evidence{f' (batch {st})' if st else ''}"
    if op == "rotate_credential":
        return P3, "destructive: breaks every job still using OPERATOR, and no credential exploit was found"
    if op == "chmod_config" and unix and (unix.get("config") or {}).get("/etc/xfer.conf") == "0640":
        return P4, "/etc/xfer.conf is already 0640"
    if op == "apply_patch":
        return P5, "no SQL injection in evidence: outside this recovery plan"
    if op == "quarantine_record":
        return P5, "no poison record in evidence"
    if op == "refresh_cache" and portal and (portal.get("cache") or {}).get("stale") is False:
        return P4, "the balance cache isn't stale (cache.stale=false); a refresh changes nothing"
    if op == "rerun_transfer" and unix and ((unix.get("transfer") or {}).get("status") == "ok"):
        return P4, "the last transfer succeeded (transfer.status=ok); nothing to re-run"
    missing = "" if rb.get(p["target"]) else " (read-back unavailable)"
    return P5, f"outside this recovery plan: diagnosis found no fault it fixes{missing}"


def judge(proposal: dict, plan_steps: list[dict], root: str, fault_class: str, readback: dict) -> dict:
    """Check Claude's proposal against the plan the policy is running. Pure function (unit-tested).
    Returns {verdicts (aligned with proposal ops), missed_ops, root_agrees, agrees_with_policy}."""
    plan = sorted(plan_steps, key=lambda s: s.get("n", 0))
    used: dict[int, int] = {}  # plan index -> proposal index
    rows = []
    for k, p in enumerate(proposal["ops"]):
        row = {"op": p["op"], "target": p["target"], "verdict": "overruled", "rule": P1, "reason": None,
               "gated": False, "note": None, "plan_n": None}
        rows.append(row)
        sys_ops = OPS.get(p["target"])
        if p["dup_params"]:
            row["reason"] = "malformed: the same parameter given twice"
        elif sys_ops is None:
            row["reason"] = f"no system '{p['target']}' in this estate"
        elif p["op"] not in sys_ops:
            other = next((s for s, ops in OPS.items() if p["op"] in ops), None)
            row["reason"] = f"{p['op']} runs on {other}, not {p['target']}" if other else \
                f"no op '{p['op']}' on {p['target']}"
        elif set(p["params"]) - set(sys_ops[p["op"]]):
            row["reason"] = f"unknown parameter {sorted(set(p['params']) - set(sys_ops[p['op']]))[0]} for {p['op']}"
        else:
            cands = [i for i, s in enumerate(plan) if (s["system"], s["op"]) == (p["target"], p["op"])]
            free = [i for i in cands if i not in used]
            fits = [i for i in free if all(_same(p["op"], kk, v, (plan[i].get("params") or {}).get(kk, v))
                                           for kk, v in p["params"].items())]
            if fits:
                i = fits[0]
                used[i] = k
                s = plan[i]
                gated = bool(s.get("destructive") or (s["system"], s["op"]) in policy.DESTRUCTIVE)
                row.update(verdict="accepted", plan_n=s.get("n"), gated=gated, rule=P6 if gated else OK,
                           reason="destructive: the Beekeeper approves before it is sent" if gated else "matches the policy's plan")
                if gated and p["risk"] != "destructive":
                    row["note"] = "Claude rated it safe; it is destructive"
                elif set(s.get("params") or {}) - set(p["params"]):
                    row["note"] = "params completed by policy"
            elif free:
                kk, v = next((kk, v) for kk, v in p["params"].items()
                             if not _same(p["op"], kk, v, (plan[free[0]].get("params") or {}).get(kk, v)))
                if p["op"] == "quarantine_record":
                    recs = [str((plan[i].get("params") or {}).get("record_id")) for i in cands]
                    row.update(rule=P4, reason=f"{v} isn't a poison record; diagnosis found {', '.join(recs)}")
                else:
                    row["reason"] = f"{kk}={v} is wrong; the only valid value is {(plan[free[0]].get('params') or {}).get(kk)}"
            elif cands:
                row["reason"] = f"duplicate: {p['op']} is already proposed"
            else:
                bad = next(((kk, v) for kk, v in p["params"].items()
                            if (p["op"], kk) in FIXED and not _same(p["op"], kk, v, FIXED[(p["op"], kk)])), None)
                if bad:
                    row["reason"] = f"{bad[0]}={bad[1]} doesn't exist (only {FIXED[(p['op'], bad[0])]})"
                else:
                    row["rule"], row["reason"] = _outside(p, readback)

    # Dependency order, in proposal order against the SURVIVING accepted matches: an accepted op is refused unless
    # every plan step of the op before it in the chain was proposed earlier and still stands. So a refused
    # prerequisite also refuses what depends on it (a transfer without a re-run batch would not help either).
    chain = list(dict.fromkeys(s["op"] for s in plan))  # distinct ops in plan order
    proposed_at = dict(used)  # plan index -> proposal index, before any dependency refusal
    for k, row in enumerate(rows):
        g = chain.index(row["op"]) if row["verdict"] == "accepted" and row["op"] in chain else 0
        if g == 0:
            continue
        pre = chain[g - 1]
        need = [i for i, s in enumerate(plan) if s["op"] == pre]
        if all(i in used and used[i] < k for i in need):
            continue
        if all(i in proposed_at and proposed_at[i] < k for i in need):
            reason = f"depends on {pre}, which was overruled"
        else:
            later = any(i in proposed_at and proposed_at[i] > k for i in need)
            reason = (f"{'runs before' if later else 'skips'} {pre}: "
                      f"{PREREQ_WHY.get((row['op'], pre), row['op'] + ' needs ' + pre + chr(39) + 's effect first')}")
        row.update(verdict="overruled", gated=False, note=None, rule=P2, reason=reason)
        used = {i: kk for i, kk in used.items() if kk != k}

    missed = [{"op": s["op"], "target": s["system"], "params": s.get("params") or {}, "n": s.get("n")}
              for i, s in enumerate(plan) if i not in used]
    root_agrees = proposal["root_system"] == root and proposal["fault_class"] == fault_class
    overruled = [r for r in rows if r["verdict"] != "accepted"]
    return {"verdicts": rows, "missed_ops": missed, "root_agrees": root_agrees,
            "agrees_with_policy": bool(root_agrees and not overruled and not missed and rows)}


def summary(ai: dict, root: str, fault_class: str) -> str:
    n, bad = len(ai["verdicts"]), [v for v in ai["verdicts"] if v["verdict"] != "accepted"]
    if ai["agrees_with_policy"]:
        return f"Second opinion (Claude): same root cause ({root}, {fault_class}) and all {n} ops match the plan."
    parts = [f"agrees on root cause {root}/{fault_class}" if ai["root_agrees"] else
             f"says {ai.get('root_system')}/{ai.get('fault_class')}, policy says {root}/{fault_class}"]
    if bad:
        parts.append(f"guardrail overruled {len(bad)} of {n} ops ({bad[0]['op']} → {bad[0]['rule']}: {bad[0]['reason']})")
    if ai["missed_ops"]:
        parts.append(f"policy adds {', '.join(m['op'] for m in ai['missed_ops'])}")
    return "Second opinion (Claude): " + "; ".join(parts) + "."


def bump_tally(verdicts: list[dict]) -> dict:
    acc = sum(v["verdict"] == "accepted" for v in verdicts)
    doc = col("stats").find_one_and_update(
        {"_id": "ai_tally"},
        {"$inc": {"proposed": len(verdicts), "accepted": acc, "overruled": len(verdicts) - acc, "incidents": 1},
         "$set": {"updated_at": now()}},
        upsert=True, return_document=ReturnDocument.AFTER)
    return {k: int(doc.get(k) or 0) for k in ("proposed", "accepted", "overruled", "incidents")}


# ---------------------------------------------------------------------------------------------- consult
def _gather(left) -> tuple[dict, dict, dict]:
    """Ops catalog, read-backs and recent logs per system, within the time left (fallbacks on any error)."""
    catalog, readback, logs = {}, {}, {}
    reserve = 2.5  # keep this much of the budget for Claude
    sysc = Systems(timeout=1.0)
    try:
        for s in config.SYSTEM_IDS:
            for name, fn, fallback in (("ops", sysc.ops, static_catalog()[s]), ("state", sysc.state, None),
                                       ("logs", lambda x: sysc.logs(x, limit=LOG_LINES), [])):
                spare = left() - reserve
                val = fallback
                if spare > 0.05:  # checked before EVERY request, each bounded by what is left
                    try:
                        getattr(sysc, "set_timeout", lambda _t: None)(min(1.0, spare))
                        val = fn(s)
                    except Exception:
                        val = fallback
                {"ops": catalog, "state": readback, "logs": logs}[name][s] = val or fallback
    finally:
        sysc.close()
    return catalog, readback, logs


def consult(ev: dict, plan: list[dict], root: str, fault_class: str, budget_s: float, llm_timeout: float,
            task: str) -> dict:
    """Evidence snapshot → guarded Claude call → judge. Returns the `ai` fields (status done|offline)."""
    t0 = time.monotonic()
    left = lambda: budget_s - (time.monotonic() - t0)  # noqa: E731
    catalog, readback, logs = _gather(left)
    evidence_at = now()
    evidence = number_evidence(ev, logs)
    window = min(llm_timeout, left())  # the time Claude actually gets (reported on the card if it runs out)
    if window < 1.0:
        out, meta = None, {"outcome": "timeout"}
    else:
        out, meta = llm.guarded_json(task, SYSTEM_PROMPT, build_prompt(ev, evidence, catalog), _schema(),
                                     max_tokens=1100, timeout=window)
    latency = int((time.monotonic() - t0) * 1000)
    if out is None:
        return {"status": "offline", "latency_ms": latency, "guard": meta["outcome"],
                "error": {"timeout": f"no answer within {max(window, 0):.1f}s", "schema": "answer failed schema validation",
                          "bad_json": "answer was not valid JSON", "refusal": "model declined",
                          "truncated": "answer was cut off"}.get(meta["outcome"], "LLM call failed")}
    prop = normalize(out, {e["id"] for e in evidence})
    verdict = judge(prop, plan, root, fault_class, readback)
    cited = set(prop["cites"]) | {c for h in prop["hypotheses"] for c in h["evidence"]}
    return {"status": "done", "guard": "ok", "hypothesis": prop["hypothesis"], "hypotheses": prop["hypotheses"],
            "cites": prop["cites"], "confidence": prop["confidence"], "latency_ms": latency,
            "proposed_ops": [{k: o[k] for k in ("op", "target", "params", "risk", "why")} for o in prop["ops"]],
            **verdict, "root_system": prop["root_system"], "fault_class": prop["fault_class"],
            "policy": {"root_system": root, "fault_class": fault_class},
            "evidence": [e for e in evidence if e["id"] in cited], "evidence_at": evidence_at, "done_at": now()}


def _finish(agent_id: str, inc_id: str, mine: dict, ai: dict) -> dict | None:
    """Store the verdict, bump the tally (+ LLM fallbacks), return the stored doc or None if superseded."""
    if col("incidents").update_one(mine, {"$set": {"ai": ai}}).matched_count != 1:
        return None
    try:
        tally = bump_tally(ai.get("verdicts") or []) if ai["status"] == "done" else None
        g = col("stats").find_one({"_id": "llm_guard"}) or {}
        allc = g.get("all") or {}
        tally = {**(tally or {k: 0 for k in ("proposed", "accepted", "overruled", "incidents")}),
                 "llm_calls": int(allc.get("calls") or 0),
                 "llm_fallbacks": int(sum(v for k, v in allc.items() if k not in ("ok", "calls")))}
        col("incidents").update_one(mine, {"$set": {"ai.tally": tally}})
        ai["tally"] = tally
    except Exception:
        pass
    return ai


# ---------------------------------------------------------------------------------------------- AI drives
def hypothesis_lines(ai: dict) -> list[str]:
    out = []
    for n, h in enumerate(ai.get("hypotheses") or [], 1):
        cites = f" [{', '.join(h['evidence'])}]" if h.get("evidence") else ""
        out.append(f"Claude H{n}{' (most likely)' if n == 1 else ''}: {h['system']}/{h['fault_class']} — {h['why']}{cites}")
    return out


def decide(agent_id: str, inc: dict, state: dict, rules_plan: list[dict], root: str, fault_class: str,
           say=lambda text: None) -> dict:
    """AI_DRIVES: ask Claude, with ONE hard deadline (AI_DRIVE_TIMEOUT_S) for evidence + inference. If its plan
    passes every guardrail it is the plan that runs; otherwise, or when the deadline passes, the rules plan runs
    and `ai.decision_reason` says why. A late answer is discarded. Never raises.
    Returns {"plan": [steps for the scout], "decision": "ai_plan"|"rules_plan", "reason": str}."""
    rules = [dict(s, source="policy") for s in rules_plan]
    cap = config.AI_DRIVE_TIMEOUT_S
    t0 = time.monotonic()
    rid = uuid.uuid4().hex[:12]
    mine = {"_id": inc.get("_id"), "opened_at": inc.get("opened_at"), "ai.request_id": rid}
    try:
        inc_id, opened_at = inc["_id"], inc["opened_at"]
        base = {"kind": KIND, "model": config.LLM_MODEL, "request_id": rid, "started_at": now(), "drives": True}
        with pymongo.timeout(1.0):
            r = col("incidents").update_one(  # never clobber another AI feature's doc (e.g. W3's schema_mapping)
                {"_id": inc_id, "opened_at": opened_at, "$or": [{"ai": {"$exists": False}}, {"ai.kind": KIND}]},
                {"$set": {"ai": {**base, "status": "thinking"}}})
        if r.matched_count != 1:
            return {"plan": rules, "decision": "rules_plan", "reason": "another AI step already owns this incident"}
        plan_n = [dict(s, n=i) for i, s in enumerate(rules_plan, 1)]
        ev = findings_of(inc, state)
        box: queue.Queue = queue.Queue(maxsize=1)

        def work():  # evidence + Claude + judge; touches no incident state
            try:
                box.put(consult(ev, plan_n, root, fault_class, budget_s=cap - (time.monotonic() - t0),
                                llm_timeout=cap, task="ai_drive"))
            except Exception as e:  # noqa: BLE001
                box.put({"status": "offline", "guard": "error", "error": type(e).__name__,
                         "latency_ms": int((time.monotonic() - t0) * 1000)})
        threading.Thread(target=work, name=f"ai-drive-{inc_id}", daemon=True).start()
        say(f"Asking Claude to reason over the evidence (waits at most {cap:.1f}s, evidence included); its plan "
            f"runs only if it passes every guardrail.")
        try:
            got = box.get(timeout=max(0.1, cap - (time.monotonic() - t0)))
        except queue.Empty:  # deadline: whatever arrives later is ignored
            got = {"status": "offline", "guard": "timeout", "error": f"no decision within {cap:.1f}s",
                   "latency_ms": int((time.monotonic() - t0) * 1000)}
        ai = {**base, **got}
        if ai["status"] != "done":
            decision, reason = "rules_plan", f"Claude unavailable ({ai['error']}); rules plan runs"
        else:
            for line in hypothesis_lines(ai):
                say(line)
            ops = " → ".join(f"{o['op']}@{o['target']}" for o in ai["proposed_ops"]) or "nothing"
            say(f"Claude proposes: {ops} (confidence {ai['confidence'] if ai['confidence'] is not None else '?'})")
            bad = [v for v in ai["verdicts"] if v["verdict"] != "accepted"]
            if not ai["root_agrees"]:
                decision, reason = "rules_plan", (f"Claude chose {ai['root_system']}/{ai['fault_class']} but the "
                                                  f"evidence walk proves {root}/{fault_class}; rules plan runs")
            elif bad:
                decision, reason = "rules_plan", f"overruled {bad[0]['op']} → {bad[0]['rule']}: {bad[0]['reason']}; rules plan runs"
            elif ai["missed_ops"]:
                decision, reason = "rules_plan", (f"Claude's plan is incomplete (misses "
                                                  f"{', '.join(m['op'] for m in ai['missed_ops'])}); rules plan runs")
            elif not ai["verdicts"]:
                decision, reason = "rules_plan", "Claude proposed no ops; rules plan runs"
            elif any(v.get("note") == "params completed by policy" for v in ai["verdicts"]):
                filled = [v["op"] for v in ai["verdicts"] if v.get("note") == "params completed by policy"]
                decision, reason = "rules_plan", (f"Claude left params of {', '.join(filled)} to the policy, so it isn't "
                                                  f"Claude's plan; rules plan runs")
            else:
                decision, reason = "ai_plan", "Claude's plan passed P1–P6; executing it, the Guard verifies every step"
        ai.update(decision=decision, decision_reason=reason)
        try:
            with pymongo.timeout(1.5):  # bookkeeping is bounded too; the decision stands either way
                _finish(agent_id, inc_id, mine, ai)
        except Exception:
            pass
        say(f"Guardrail: {reason}.")
        if decision == "ai_plan":
            by_n = {v["plan_n"]: (v, o) for v, o in zip(ai["verdicts"], ai["proposed_ops"])}
            plan = []
            for s in plan_n:  # Claude's ops, in Claude's (validated) order == the chain; exact params from read-back
                v, o = by_n[s["n"]]
                plan.append({**{k: s[k] for k in ("system", "op", "params", "desc", "destructive")},
                             "source": "ai", "ai_why": llm.clean(o.get("why"), 140)})
            return {"plan": plan, "decision": decision, "reason": reason}
        return {"plan": rules, "decision": decision, "reason": reason}
    except Exception as e:
        reason = f"AI step failed ({type(e).__name__}); rules plan runs"
        _log(agent_id, f"AI drive failed, rules plan runs: {type(e).__name__}: {e}")
        try:  # don't leave the card on "thinking" while the rules plan already runs
            with pymongo.timeout(1.0):
                col("incidents").update_one(mine, {"$set": {"ai.status": "offline", "ai.error": type(e).__name__,
                                                            "ai.decision": "rules_plan", "ai.decision_reason": reason}})
        except Exception:
            pass
        return {"plan": rules, "decision": "rules_plan", "reason": reason}


# ---------------------------------------------------------------------------------------------- background
def start(agent_id: str, inc: dict, state: dict, plan_steps: list[dict], root: str, fault_class: str) -> bool:
    """Fire-and-forget advisory second opinion. Returns at once; never raises into the caller."""
    if not enabled():
        return False
    got = False
    try:
        got = _slots.acquire(blocking=False)
        if not got:
            return False
        args = (agent_id, inc["_id"], inc["opened_at"], findings_of(inc, state), [dict(s) for s in plan_steps],
                root, fault_class)
        threading.Thread(target=_run, args=args, name=f"ai-{inc['_id']}", daemon=True).start()
        return True
    except Exception as e:
        if got:
            _slots.release()
        _log(agent_id, f"second opinion not started: {type(e).__name__}: {e}")
        return False


def _log(agent_id: str, msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} [{agent_id}] {msg}", flush=True)


def _run(agent_id: str, inc_id: str, opened_at, ev: dict, plan: list[dict], root: str, fault_class: str) -> None:
    try:
        with pymongo.timeout(DEADLINE_S + 5):  # bounds every MongoDB call in this thread too
            _opinion(agent_id, inc_id, opened_at, ev, plan, root, fault_class)
    finally:
        _slots.release()


def _opinion(agent_id: str, inc_id: str, opened_at, ev: dict, plan: list[dict], root: str, fault_class: str) -> None:
    rid = uuid.uuid4().hex[:12]
    mine = {"_id": inc_id, "opened_at": opened_at, "ai.request_id": rid}  # a reset may reuse the INC id
    base = {"kind": KIND, "model": config.LLM_MODEL, "request_id": rid, "started_at": now()}
    try:
        r = col("incidents").update_one({"_id": inc_id, "opened_at": opened_at, "ai": {"$exists": False}},
                                        {"$set": {"ai": {**base, "status": "thinking"}}})
        if r.matched_count != 1:
            return
        ai = {**base, **consult(ev, plan, root, fault_class, DEADLINE_S, LLM_TIMEOUT_S, task="second_opinion")}
        stored = _finish(agent_id, inc_id, mine, ai)
        if stored is None:
            return
        if ai["status"] != "done":
            _log(agent_id, f"{inc_id} second opinion offline after {ai['latency_ms']} ms ({ai['guard']})")
            return
        text = summary(ai, root, fault_class)
        events.think(agent_id, "scout", text, incident=inc_id, system=root, kind="thought")
        _log(agent_id, f"(ai {ai['latency_ms']} ms) {text}")
    except Exception as e:
        _log(agent_id, f"{inc_id} second opinion failed: {type(e).__name__}: {e}")
        try:
            col("incidents").update_one(mine, {"$set": {"ai.status": "offline", "ai.error": type(e).__name__}})
        except Exception:
            pass
