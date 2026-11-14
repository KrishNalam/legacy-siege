"""AI second opinion: Claude proposes, verification disposes.

After the Scout writes its plan, `start()` spawns ONE background thread per incident that asks Claude for an
independent root-cause hypothesis and op plan, built only from the findings the Hive collected (never the
policy's own conclusion). A deterministic judge then compares every proposed op with the plan the policy is
actually running and with read-backs taken BEFORE the call: agree, overruled (with a concrete reason), or
missed ("policy adds"). The result lands on the incident as the optional, advisory `ai` field.

Nothing here is ever executed and no decision path reads `incidents.ai`. With no key, at capacity, on timeout
or on any error, the incident simply has no `ai` (or `ai.status == "offline"`): the demo runs exactly as before.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime

import pymongo

from common import config, events, llm
from common.db import col, now

from . import policy, settings
from .http import Systems

DEADLINE_S = 20.0          # end-to-end budget for read-backs + LLM + writes
LLM_TIMEOUT_S = 12.0
MAX_OPS = 8
_slots = threading.BoundedSemaphore(2)  # at most two calls in flight per process; never queue

# Ops catalog (CONTRACTS §2): system -> op -> allowed param names, plus the only valid value of fixed params.
OPS = {
    "mainframe": {"quarantine_record": ["record_id"], "restart_batch": ["job"], "rotate_credential": ["account"]},
    "unix": {"purge_spool": ["path"], "restart_cron": ["job"], "chmod_config": ["file", "mode"], "rerun_transfer": []},
    "portal": {"flush_sessions": [], "apply_patch": ["patch"], "refresh_cache": []},
}
FIXED = {("restart_batch", "job"): "NIGHTBAT", ("rotate_credential", "account"): "OPERATOR",
         ("purge_spool", "path"): "/var/spool/xfer", ("restart_cron", "job"): "xfer_pull",
         ("chmod_config", "file"): "/etc/xfer.conf", ("chmod_config", "mode"): "0640",
         ("apply_patch", "patch"): "parameterize_search"}
DESTRUCTIVE_OPS = {op for _s, op in policy.DESTRUCTIVE}

# Every fix plan is a dependency chain (each op needs the previous one's effect). Specific reasons for the
# (op, prerequisite) pairs the plans contain; anything else gets a generic one.
PREREQ_WHY = {
    ("restart_batch", "quarantine_record"): "poison record still queued: NIGHTBAT would ABEND S0C7 again",
    ("rerun_transfer", "restart_batch"): "no fresh batch file yet: NIGHTBAT hasn't re-run",
    ("rerun_transfer", "purge_spool"): "spool still full: the transfer would fail again",
    ("rerun_transfer", "restart_cron"): "xfer_pull still stopped: nothing would pull the file",
    ("refresh_cache", "rerun_transfer"): "would reload the same stale balances: the transfer hasn't re-run",
}

SYSTEM_PROMPT = (
    "You are a senior SRE giving a second opinion on a live incident in a bank's legacy estate "
    "(mainframe -> unix middleware -> web portal; each system depends on the one before it). "
    "From the evidence only, state the root cause and the ops you would run, in order, using only ops from the "
    "catalog. Be terse: hypothesis is one sentence under 30 words; each 'why' under 12 words; at most 6 ops.")


def _schema() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["root_system", "fault_class", "hypothesis", "proposed_ops"],
        "properties": {
            "root_system": {"type": "string", "enum": list(config.SYSTEM_IDS)},
            "fault_class": {"type": "string", "enum": sorted(policy.WEAKNESS) + ["other"]},
            "hypothesis": {"type": "string"},
            "proposed_ops": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["system", "op", "params", "why"],
                "properties": {
                    "system": {"type": "string"}, "op": {"type": "string"}, "why": {"type": "string"},
                    "params": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False, "required": ["name", "value"],
                        "properties": {"name": {"type": "string"}, "value": {"type": "string"}}}},
                }}},
        },
    }


# ---------------------------------------------------------------------------------------------- prompt
def evidence(inc: dict, state: dict) -> dict:
    """What the Hive collected, minus its own conclusions (no fault_class / root_cause / plan)."""
    findings = {}
    for sys, f in (state.get("findings") or {}).items():
        f = f or {}
        findings[sys] = {"status": f.get("status"), "blames": f.get("blames"), "facts": list(f.get("facts") or [])[:6]}
    return {"symptoms": list(inc.get("symptoms") or [])[:6], "path": list(state.get("path") or []),
            "findings": findings, "affected": sorted(inc.get("affected") or [], key=policy.rank)}


def build_prompt(ev: dict, catalog: dict) -> str:
    lines = ["EVIDENCE"]
    lines += [f"- symptom: {s}" for s in ev["symptoms"]]
    if ev["affected"]:
        lines.append(f"- affected systems: {', '.join(ev['affected'])}")
    if ev["path"]:
        lines.append(f"- systems examined: {' -> '.join(ev['path'])}")
    for sys, f in ev["findings"].items():
        lines.append(f"- {sys}: status={f['status']} blames={f['blames'] or 'nothing'}"
                     + (f"; {'; '.join(str(x) for x in f['facts'])}" if f["facts"] else ""))
    lines.append("\nOPS CATALOG (system.op {params} — description)")
    for sys in config.SYSTEM_IDS:
        for o in catalog.get(sys) or []:
            p = ", ".join(f"{k}={v}" for k, v in (o.get("params") or {}).items())
            lines.append(f"- {sys}.{o['op']} {{{p}}} — {str(o.get('desc') or '')[:90]}"
                         + (" [DESTRUCTIVE]" if o.get("destructive") else ""))
    return "\n".join(lines)


def static_catalog() -> dict:
    return {s: [{"op": op, "params": {k: FIXED.get((op, k), "<value>") for k in ks},
                 "destructive": op in DESTRUCTIVE_OPS, "desc": ""} for op, ks in ops.items()]
            for s, ops in OPS.items()}


# ---------------------------------------------------------------------------------------------- judge
def _norm(s) -> str:
    return str(s or "").strip().lower()


def normalize(out: dict) -> dict:
    """Validate and tidy Claude's JSON. Anything malformed becomes something the judge can overrule."""
    ops = []
    for p in (out.get("proposed_ops") or [])[:MAX_OPS]:
        if not isinstance(p, dict):
            continue
        system, op = _norm(p.get("system")), _norm(p.get("op"))
        if "." in op:  # "unix.restart_cron"
            pre, _, op = op.partition(".")
            system = system or pre
        names = [_norm(x.get("name")) for x in p.get("params") or [] if isinstance(x, dict)]
        params = {_norm(x.get("name")): str(x.get("value") or "").strip()
                  for x in p.get("params") or [] if isinstance(x, dict) and _norm(x.get("name"))}
        ops.append({"system": system, "op": op, "params": params, "why": str(p.get("why") or "").strip()[:140],
                    "dup_params": len(names) != len(set(names))})
    return {"root_system": _norm(out.get("root_system")), "fault_class": _norm(out.get("fault_class")),
            "hypothesis": str(out.get("hypothesis") or "").strip()[:300], "ops": ops}


def _same(op: str, k: str, a, b) -> bool:
    """Exact match (after trimming): the systems reject `nightbat` or `/VAR/SPOOL/XFER`."""
    return str(a).strip() == str(b).strip()


def _outside_reason(p: dict, rb: dict) -> str:
    """Why an op that isn't in the plan was refused — from read-backs taken before Claude answered."""
    op = p["op"]
    unix, portal, mf = rb.get("unix"), rb.get("portal"), rb.get("mainframe")
    if op == "restart_cron":
        if unix and (unix.get("cron") or {}).get("xfer_pull") == "running":
            return "read-back: xfer_pull is already running; a restart would interrupt a live transfer"
    if op == "flush_sessions" and portal and portal.get("sessions") is not None \
            and portal["sessions"] < settings.SESSION_HIGH:
        return (f"read-back: {portal['sessions']} sessions (limit {settings.SESSION_HIGH}); would log out every "
                f"signed-in customer for nothing")
    if op == "purge_spool" and unix and ((unix.get("spool") or {}).get("disk_pct") or 0) < settings.DISK_HIGH:
        return f"read-back: spool at {unix['spool']['disk_pct']}%; a purge would delete in-flight transfer files"
    if op == "restart_batch":
        st = ((mf or {}).get("batch") or {}).get("status")
        return f"destructive core-ledger restart with no batch fault in evidence{f' (batch {st})' if st else ''}"
    if op == "rotate_credential":
        return "destructive: breaks every job still using OPERATOR, and no credential exploit was found"
    if op == "chmod_config" and unix and (unix.get("config") or {}).get("/etc/xfer.conf") == "0640":
        return "read-back: /etc/xfer.conf is already 0640"
    if op == "apply_patch":
        return "no SQL injection in evidence: outside this recovery plan"
    if op == "quarantine_record":
        return "no poison record in evidence"
    if op in ("rerun_transfer", "refresh_cache"):
        return f"{p['system']} isn't on this incident's fault path"
    missing = "" if rb.get(p["system"]) else " (read-back unavailable)"
    return f"outside this recovery plan: diagnosis found no fault it fixes{missing}"


def judge(proposal: dict, plan_steps: list[dict], root: str, fault_class: str, readback: dict) -> dict:
    """Compare Claude's proposal with the plan the policy is running. Pure function (unit-tested)."""
    plan = sorted(plan_steps, key=lambda s: s.get("n", 0))
    used: dict[int, int] = {}  # plan index -> proposal index
    rows = []
    for k, p in enumerate(proposal["ops"]):
        row = {"system": p["system"], "op": p["op"], "params": p["params"], "why": p["why"],
               "verdict": "overruled", "reason": None, "note": None, "gated": False, "plan_n": None}
        rows.append(row)
        sys_ops = OPS.get(p["system"])
        if p["dup_params"]:
            row["reason"] = "malformed: the same parameter given twice"
        elif sys_ops is None:
            row["reason"] = f"no system '{p['system']}' in this estate"
        elif p["op"] not in sys_ops:
            other = next((s for s, ops in OPS.items() if p["op"] in ops), None)
            row["reason"] = f"{p['op']} runs on {other}, not {p['system']}" if other else \
                f"no op '{p['op']}' on {p['system']}"
        elif set(p["params"]) - set(sys_ops[p["op"]]):
            row["reason"] = f"unknown parameter {sorted(set(p['params']) - set(sys_ops[p['op']]))[0]} for {p['op']}"
        else:
            cands = [i for i, s in enumerate(plan) if (s["system"], s["op"]) == (p["system"], p["op"])]
            free = [i for i in cands if i not in used]
            fits = [i for i in free if all(_same(p["op"], kk, v, (plan[i].get("params") or {}).get(kk, v))
                                           for kk, v in p["params"].items())]
            if fits:
                i = fits[0]
                used[i] = k
                s = plan[i]
                row.update(verdict="agree", plan_n=s.get("n"),
                           gated=bool(s.get("destructive") or (s["system"], s["op"]) in policy.DESTRUCTIVE))
                if set(s.get("params") or {}) - set(p["params"]):
                    row["note"] = "params completed by policy"
            elif free:
                kk, v = next((kk, v) for kk, v in p["params"].items()
                             if not _same(p["op"], kk, v, (plan[free[0]].get("params") or {}).get(kk, v)))
                if p["op"] == "quarantine_record":
                    recs = [str((plan[i].get("params") or {}).get("record_id")) for i in cands]
                    row["reason"] = f"{v} isn't a poison record; diagnosis found {', '.join(recs)}"
                else:
                    row["reason"] = f"{kk}={v} is wrong; the only valid value is {(plan[free[0]].get('params') or {}).get(kk)}"
            elif cands:
                row["reason"] = f"duplicate: {p['op']} is already proposed"
            else:
                bad = next(((kk, v) for kk, v in p["params"].items()
                            if (p["op"], kk) in FIXED and not _same(p["op"], kk, v, FIXED[(p["op"], kk)])), None)
                row["reason"] = (f"{bad[0]}={bad[1]} doesn't exist (only {FIXED[(p['op'], bad[0])]})" if bad
                                 else _outside_reason(p, readback))

    # Dependency order, judged on the matches above (so one misplaced op costs one cross, not a cascade):
    # a matched op is refused unless every plan step of the op before it in the chain was proposed earlier.
    chain = list(dict.fromkeys(s["op"] for s in plan))  # distinct ops in plan order
    matched = dict(used)
    for k, row in enumerate(rows):
        g = chain.index(row["op"]) if row["verdict"] == "agree" and row["op"] in chain else 0
        if g == 0:
            continue
        pre = chain[g - 1]
        need = [i for i, s in enumerate(plan) if s["op"] == pre]
        if not all(i in matched and matched[i] < k for i in need):
            later = any(i in matched and matched[i] > k for i in need)
            why = PREREQ_WHY.get((row["op"], pre), f"{row['op']} needs {pre}'s effect first")
            row.update(verdict="overruled", gated=False, note=None,
                       reason=f"{'out of order' if later else f'{pre} missing'}: {why}")
            used = {i: kk for i, kk in used.items() if kk != k}

    missed = [{"system": s["system"], "op": s["op"], "params": s.get("params") or {}, "n": s.get("n")}
              for i, s in enumerate(plan) if i not in used]
    overruled = [{"op": r["op"], "system": r["system"], "reason": r["reason"]} for r in rows if r["verdict"] != "agree"]
    root_agrees = proposal["root_system"] == root and proposal["fault_class"] == fault_class
    return {"root_agrees": root_agrees, "proposed_ops": rows, "overruled_ops": overruled, "missed_ops": missed,
            "agrees_with_policy": bool(root_agrees and not overruled and not missed and rows)}


def summary(ai: dict, root: str, fault_class: str) -> str:
    n, bad = len(ai["proposed_ops"]), ai["overruled_ops"]
    if ai["agrees_with_policy"]:
        return f"Second opinion (Claude): same root cause ({root}, {fault_class}) and all {n} ops match the plan."
    parts = [f"agrees on root cause {root}/{fault_class}" if ai["root_agrees"] else
             f"says {ai.get('root_system')}/{ai.get('fault_class')}, policy says {root}/{fault_class}"]
    if bad:
        parts.append(f"guardrail overruled {len(bad)} of {n} ops ({bad[0]['op']}: {bad[0]['reason']})")
    if ai["missed_ops"]:
        parts.append(f"policy adds {', '.join(m['op'] for m in ai['missed_ops'])}")
    return "Second opinion (Claude): " + "; ".join(parts) + "."


# ---------------------------------------------------------------------------------------------- thread
def start(agent_id: str, inc: dict, state: dict, plan_steps: list[dict], root: str, fault_class: str) -> bool:
    """Fire-and-forget. Returns at once; never raises into the caller."""
    if not llm.llm_available():
        return False
    got = False
    try:
        got = _slots.acquire(blocking=False)
        if not got:
            return False
        args = (agent_id, inc["_id"], inc["opened_at"], evidence(inc, state), [dict(s) for s in plan_steps],
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
    t0 = time.monotonic()
    rid = uuid.uuid4().hex[:12]
    mine = {"_id": inc_id, "opened_at": opened_at, "ai.request_id": rid}  # a reset may reuse the INC id
    base = {"model": config.LLM_MODEL, "request_id": rid, "started_at": now()}
    try:
        r = col("incidents").update_one({"_id": inc_id, "opened_at": opened_at, "ai": {"$exists": False}},
                                        {"$set": {"ai": {**base, "status": "thinking"}}})
        if r.matched_count != 1:
            return
        left = lambda: DEADLINE_S - (time.monotonic() - t0)  # noqa: E731
        catalog, readback = {}, {}
        sysc = Systems(timeout=2.0)
        try:
            for s in config.SYSTEM_IDS:  # read-backs get at most ~half the budget; the LLM keeps the rest
                fast = left() > DEADLINE_S / 2
                try:
                    catalog[s] = sysc.ops(s) if fast else static_catalog()[s]
                except Exception:
                    catalog[s] = static_catalog()[s]
                try:
                    readback[s] = sysc.state(s) if fast else None
                except Exception:
                    readback[s] = None
        finally:
            sysc.close()
        evidence_at = now()
        out = llm.complete_json(SYSTEM_PROMPT, build_prompt(ev, catalog), _schema(), max_tokens=900,
                                timeout=min(LLM_TIMEOUT_S, left() - 1)) if left() > 4 else None
        if left() < 0:  # answered after the deadline: too late to show as live analysis
            out = None
        latency = int((time.monotonic() - t0) * 1000)
        if out is None:
            col("incidents").update_one(mine, {"$set": {"ai": {**base, "status": "offline", "latency_ms": latency,
                                                               "error": "no answer (timeout, refusal or error)"}}})
            _log(agent_id, f"{inc_id} second opinion offline after {latency} ms")
            return
        prop = normalize(out)
        verdict = judge(prop, plan, root, fault_class, readback)
        ai = {**base, "status": "done", "hypothesis": prop["hypothesis"], "root_system": prop["root_system"],
              "fault_class": prop["fault_class"], **verdict, "policy": {"root_system": root, "fault_class": fault_class},
              "evidence_at": evidence_at, "latency_ms": latency, "done_at": now()}
        if col("incidents").update_one(mine, {"$set": {"ai": ai}}).matched_count == 1:
            text = summary(ai, root, fault_class)
            events.think(agent_id, "scout", text, incident=inc_id, system=root, kind="thought")
            _log(agent_id, f"(ai {latency} ms) {text}")
    except Exception as e:
        _log(agent_id, f"{inc_id} second opinion failed: {type(e).__name__}: {e}")
        try:
            col("incidents").update_one(mine, {"$set": {"ai.status": "offline", "ai.error": type(e).__name__}})
        except Exception:
            pass
