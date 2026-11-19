"""Workflow drift: a legacy system changes HOW an op must be run (a new prerequisite step). Switch WORKFLOW_DRIFT.

Statement 3, question 3 ("adapt when a system's ... workflow changes underneath it"). schema.py adapts to a renamed
field; this module adapts to a changed procedure, e.g. after JES2 maintenance the mainframe refuses
``restart_batch`` with ``JCL HOLD: issue release_hold before restart_batch`` and lists a new op ``release_hold``.

  NOTICE   a worker's step comes back refused (accepted:false) with a telling error.
  WORK OUT the worker reads the system's live self-description (``GET /{sys}/ops``). Deterministic rules decide:
           R1 the refusal names the op, R2 the live catalog lists it as a prerequisite of the refused op
           (``requires`` on the op, or ``before`` on the new op), R3 it is not destructive, R4 its params are fully
           given by the catalog. Claude may propose (key + AI_SECOND_OPINION) but the same rules accept or overrule it.
           The new step is inserted before the refused one, in the same fenced task (one transaction).
  VERIFY   the Guard verifies the new step and the retried op by read-back, like every step.
  REMEMBER only after the Guard verified the retried op: ``swarm.schema_mappings`` gets
           ``{_id: "<sys>.workflow.<op>", kind: "workflow", requires, version, history[]}`` next to the field
           mappings (v1 = the original workflow). A change verified before keeps its version (``reused``).
  REPEAT   next time the Scout plans that op, the learned prerequisite goes in up front (after checking the live
           catalog still lists it): no refusal, no extra round trip.
Off (default): no hook does anything; a refused op is rejected and retried exactly as before.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from pymongo import ReturnDocument

from common import config, llm
from common.db import col, now, txn

from . import leases

PREREQ_RE = re.compile(r"issue\s+([a-z_][a-z0-9_]*)\s+before\s+([a-z_][a-z0-9_]*)", re.I)
RULE = "R1 refusal names it + R2 live /ops lists it as a prerequisite + R3 non-destructive + R4 params given"
AI_TIMEOUT_S = float(os.environ.get("WORKFLOW_AI_TIMEOUT_S", 6))


def _on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    return _on("WORKFLOW_DRIFT")


def ai_enabled() -> bool:
    return llm.llm_available() and _on("AI_SECOND_OPINION")


def mid(system: str, op: str) -> str:
    return f"{system}.workflow.{op}"


def refusal(step: dict) -> str | None:
    """The system's own error text if this attempt was refused outright (accepted:false)."""
    resp = step.get("response") or {}
    if resp.get("accepted") is False and resp.get("message") and not resp.get("error"):
        return str(resp["message"])
    return None


# ---------------------------------------------------------------- work out the new workflow (rules decide)
def check(op: str, message: str, catalog: list[dict], candidate: str) -> tuple[bool, str]:
    """Deterministic validation of one proposed prerequisite (R1-R4). Returns (ok, reason)."""
    cat = {o.get("op"): o for o in catalog or []}
    named = {m.group(1) for m in PREREQ_RE.finditer(message or "") if m.group(2) == op}
    if candidate not in named:
        return False, f"R1: the refusal does not name {candidate}"
    spec = cat.get(candidate)
    if spec is None:
        return False, f"R2: {candidate} is not in the live /ops catalog"
    if candidate not in (cat.get(op, {}).get("requires") or []) and spec.get("before") != op:
        return False, f"R2: /ops does not list {candidate} as a prerequisite of {op}"
    if spec.get("destructive"):
        return False, f"R3: {candidate} is destructive (needs a human, not an automatic insert)"
    params = spec.get("params") or {}
    if any(isinstance(v, str) and v.startswith("<") for v in params.values()):
        return False, f"R4: {candidate} needs params the catalog doesn't give ({params})"
    return True, f"refusal names {candidate}; /ops lists it before {op}; non-destructive; params {params}"


def propose(op: str, message: str, catalog: list[dict]) -> dict:
    """Rules first (every op the refusal names), Claude optionally; the same check() decides either way.
    Returns {ok, op?, params?, proposed_by, reason, ai?}."""
    named = [m.group(1) for m in PREREQ_RE.finditer(message or "") if m.group(2) == op]
    rules = next(((c, r) for c in named for ok, r in [check(op, message, catalog, c)] if ok), None)
    ai = None
    if ai_enabled():
        ai = ask_claude(op, message, catalog)
        if ai.get("op"):
            ok, reason = check(op, message, catalog, ai["op"])
            ai["verdict"], ai["reason"] = ("accepted" if ok else "overruled"), reason
            if ok:
                spec = next(o for o in catalog if o.get("op") == ai["op"])
                return {"ok": True, "op": ai["op"], "params": dict(spec.get("params") or {}), "proposed_by": "claude",
                        "reason": reason, "ai": ai}
    if rules:
        spec = next(o for o in catalog if o.get("op") == rules[0])
        return {"ok": True, "op": rules[0], "params": dict(spec.get("params") or {}), "proposed_by": "rules",
                "reason": rules[1], **({"ai": ai} if ai else {})}
    why = check(op, message, catalog, named[0])[1] if named else "the refusal names no prerequisite op"
    return {"ok": False, "proposed_by": "rules", "reason": why, **({"ai": ai} if ai else {})}


def ask_claude(op: str, message: str, catalog: list[dict]) -> dict:
    """Ask Claude which catalog op must run before `op`. Hard time budget, never raises."""
    prompt = (f"A legacy system refused `{op}` with: {llm.fence('refusal', message)}\n"
              f"Its live ops catalog: {json.dumps(catalog, default=str)[:3000]}\n"
              'Reply as {"op": "<the catalog op that must run first>", "why": "<one sentence>"}')
    box: dict = {}
    t0 = time.monotonic()
    th = threading.Thread(target=lambda: box.update(text=llm.complete(
        "You keep legacy runbooks current. Answer with one JSON object only.", prompt, max_tokens=150)), daemon=True)
    th.start()
    th.join(AI_TIMEOUT_S)
    out = {"model": config.LLM_MODEL, "latency_ms": int((time.monotonic() - t0) * 1000)}
    m = re.search(r"\{.*\}", box.get("text") or "", re.S)
    if th.is_alive() or not m:
        return {**out, "error": "timeout" if th.is_alive() else "no answer"}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {**out, "error": "bad JSON"}
    return {**out, "op": str(data.get("op") or "").strip() or None, "why": str(data.get("why") or "")[:200]}


# ---------------------------------------------------------------- NOTICE + WORK OUT (worker, fenced on its task)
def adapt(agent, task: dict, generation: int, step: dict) -> bool:
    """A step of this task was refused. Work out the new workflow and insert the prerequisite step before it.
    Runs once per refused attempt; True if a step was inserted (the caller re-selects)."""
    if not enabled():
        return False
    msg = refusal(step)
    if not msg or step.get("workflow_checked") == step.get("action_id"):
        return False
    inc, sys, op = task["incident"], step["system"], step["op"]
    agent.think(f"{sys} refused {op} ({step['action_id']}): \"{msg[:120]}\". Not a lie, not a crash: the workflow may "
                f"have changed. Reading {sys} /ops to work out the new one.", incident=inc, system=sys, pause=False)
    try:
        catalog = agent.systems.ops(sys)
    except Exception as e:
        catalog, msg = [], msg + f" (catalog unreadable: {type(e).__name__})"
    prop = propose(op, msg, catalog)
    pre_id = f"{step['step_id']}p"
    rec = {"system": sys, "op": op, "message": msg, "refused": step["action_id"], "before_step": step["step_id"],
           "proposed_by": prop["proposed_by"], "reason": prop["reason"], "rule": RULE, "ts": now(),
           **({"ai": prop["ai"]} if prop.get("ai") else {})}
    if prop["ok"]:
        rec.update(requires=[prop["op"]], params={prop["op"]: prop["params"]}, step_id=pre_id)
    new = {"step_id": pre_id, "n": step["n"] - 0.5, "system": sys, "op": prop.get("op"), "params": prop.get("params"),
           "desc": f"New workflow: {prop.get('op')} before {op} ({sys} changed its procedure)", "destructive": False,
           "task_id": task["_id"], "status": "planned", "action_id": None, "attempts": [], "agent": None,
           "evidence": None, "approval_id": None, "ts": now(), "workflow": {"before": op, "learned": True}}

    def fn(s):
        leases.fence(col("tasks"), task["_id"], agent.id, generation, s)
        sets = {"steps.$[r].workflow_checked": step["action_id"], "workflow_drift": rec}
        col("incidents").update_one({"_id": inc}, {"$set": sets}, array_filters=[{"r.step_id": step["step_id"]}],
                                    session=s)
        if not prop["ok"]:
            return False
        r = col("incidents").update_one({"_id": inc, "steps.step_id": {"$ne": pre_id}}, {"$push": {"steps": new}},
                                        session=s)
        col("tasks").update_one({"_id": task["_id"]}, {"$addToSet": {"step_ids": pre_id}}, session=s)
        return r.modified_count == 1
    inserted = txn(fn)
    if not prop["ok"]:
        agent.think(f"Workflow check on {op}: {prop['reason']}; no safe new step, retrying as before.",
                    incident=inc, system=sys, pause=False)
        return False
    who = ("Claude proposed it, rules accepted" if prop["proposed_by"] == "claude" else "rules")
    agent.think(f"workflow changed: {op} now needs {prop['op']} first ({who}: {prop['reason']}) → inserted "
                f"{pre_id} {prop['op']} before retrying {op}.", kind="learn", incident=inc, system=sys, pause=False)
    return inserted


# ---------------------------------------------------------------- VERIFY -> REMEMBER (guard)
def learn(system: str, op: str, requires: list[str], params: dict, incident: str, step_id: str, evidence: str,
          agent: str, proposed_by: str) -> dict | None:
    """Store a Guard-verified workflow. Same change verified before -> its version (reused); else max+1 (v1 is the
    original workflow). Idempotent per incident."""
    _id = mid(system, op)
    doc = col("schema_mappings").find_one({"_id": _id}) or {}
    if doc.get("learned_from") == incident and doc.get("requires") == requires:
        return None
    seen = next((e for e in reversed(doc.get("history") or []) if e.get("requires") == requires), None)
    ver = int(seen["version"]) if seen else max([int(doc.get("version") or 1)] +
                                                [int(e.get("version") or 1) for e in doc.get("history") or []]) + 1
    entry = {"version": ver, "requires": requires, "params": params, "incident": incident, "step_id": step_id,
             "verified_by": agent, "evidence": evidence, "proposed_by": proposed_by, "reused": bool(seen), "ts": now()}
    out = col("schema_mappings").find_one_and_update(
        {"_id": _id}, {"$set": {"kind": "workflow", "system": system, "op": op, "requires": requires, "params": params,
                                "previous": doc.get("requires") or [], "version": ver, "reused": bool(seen),
                                "learned_from": incident, "verified_by": agent, "evidence": evidence,
                                "proposed_by": proposed_by, "ts": now()},
                       "$push": {"history": entry}}, upsert=True, return_document=ReturnDocument.AFTER)
    return {**entry, "_id": out["_id"]}


def on_verified(agent, incident: str, st: dict, facts: str) -> None:
    """Guard hook after a step is verified: (a) the retried op of a workflow change -> store the workflow;
    (b) a prerequisite planned from memory -> say the memory held."""
    if not enabled():
        return
    if (st.get("workflow") or {}).get("memory"):
        w = st["workflow"]
        agent.think(f"workflow memory held: {st['op']} verified before {w['before']} (v{w.get('version')} reused from "
                    f"swarm.schema_mappings, no refusal this time).", kind="learn", incident=incident,
                    system=st["system"], pause=False)
        return
    inc = col("incidents").find_one({"_id": incident}, {"workflow_drift": 1, "steps": 1}) or {}
    wd = inc.get("workflow_drift") or {}
    if not wd.get("requires") or wd.get("op") != st["op"] or wd.get("system") != st["system"]:
        return
    pre = next((s for s in inc.get("steps") or [] if s["step_id"] == wd.get("step_id")), {})
    if pre.get("status") != "verified":
        return
    m = learn(st["system"], st["op"], wd["requires"], wd.get("params") or {}, incident, st["step_id"],
              f"{pre.get('evidence')}; then {facts}", agent.id, wd.get("proposed_by", "rules"))
    if not m:
        return
    stored = f"v{m['version']} {'reused' if m['reused'] else 'stored'}"
    agent.think(f"workflow changed: {st['op']} now needs {' → '.join(wd['requires'])} first → Guard verified "
                f"({wd['requires'][0]} then {st['op']} took effect) → learned: swarm.schema_mappings {m['_id']} {stored}.",
                kind="learn", incident=incident, system=st["system"], pause=False)


# ---------------------------------------------------------------- REPEAT (scout plan)
def with_memory(raw: list[dict], read_ops) -> tuple[list[dict], list[str]]:
    """Insert learned prerequisites before the ops that need them, if the live catalog still lists them.
    Returns (plan, narration lines). Unchanged plan when off or nothing is learned."""
    if not enabled() or not raw:
        return raw, []
    docs = {d["op"]: d for d in col("schema_mappings").find(
        {"kind": "workflow", "_id": {"$in": [mid(s["system"], s["op"]) for s in raw]}})}
    if not docs:
        return raw, []
    out, lines = [], []
    for s in raw:
        d = docs.get(s["op"])
        have = {(x["system"], x["op"]) for x in out}
        if d and d.get("system") == s["system"] and d.get("requires"):
            try:
                cat = {o.get("op"): o for o in read_ops(s["system"])}
            except Exception:
                cat = {}
            live = [r for r in d["requires"] if r in cat and not cat[r].get("destructive")]
            if live == d["requires"]:
                for r in live:
                    if (s["system"], r) in have:
                        continue
                    out.append({"system": s["system"], "op": r, "destructive": False,
                                "params": dict((d.get("params") or {}).get(r) or cat[r].get("params") or {}),
                                "desc": f"Learned workflow v{d['version']}: {r} before {s['op']} (from {d.get('learned_from')})",
                                "workflow": {"before": s["op"], "memory": d["_id"], "version": d["version"]}})
                col("schema_mappings").update_one({"_id": d["_id"]}, {"$inc": {"uses": 1}, "$set": {"last_used_at": now()}})
                lines.append(f"workflow memory: {s['op']} needs {', '.join(live)} first (v{d['version']}, learned on "
                             f"{d.get('learned_from')}; live /ops still lists it) → planned up front, no refusal.")
            else:
                lines.append(f"workflow memory says {s['op']} needs {', '.join(d['requires'])}, but live /ops no longer "
                             f"lists it → planning the original workflow.")
        out.append(s)
    return out, lines
