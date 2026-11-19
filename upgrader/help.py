"""Upgrader help desk (UPGRADER_HELP=1, default OFF): two hand-offs from the swarm across
its boundary into the Upgrader (a separate system outside the swarm), one collection.

    swarm.gardener_requests  {_id, kind, from, system, status: open -> answering -> answered, answer, ts}

* kind "repeat" (Monitor #2): a problem seen 3+ times. The Upgrader notes it and, when one of its vetted
  remediations covers that system + fault class, starts its existing upgrade pipeline (files the upgrade
  request, or re-queues a rejected one); upgrader.main then runs plan -> sandbox test + replay -> approval ->
  apply -> prove exactly as for the Monitor's own 2-repeat request. Nothing about that trigger changes.
* kind "help" (an Operator whose step failed verification twice): the Upgrader answers from what it remembers
  (a verified playbook with different params for the same op, a failure lesson, its proven upgrades or a
  vetted remediation) or says "none". The Operator uses the advice on its next attempt; the Verifier still
  verifies that attempt by read-back like any other.

Requests are answered by a thread inside the Upgrader process (serve), so an upgrade waiting for approval
never blocks an Operator's question, and no extra process is started. With the switch off nothing here runs.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from pymongo import ReturnDocument

from common.db import col, next_id, now
from common.events import think

log = logging.getLogger("upgrader")

AGENT, ROLE = "upgrader-1", "upgrader"
REPEAT_AFTER = 3          # Monitor #2 threshold (the Monitor's own upgrade trigger stays at UPGRADE_AFTER=2)
POLL_S = 1.0


def enabled() -> bool:
    return os.environ.get("UPGRADER_HELP", "").strip().lower() in ("1", "true", "yes", "on")


def wait_s() -> float:
    """How long an Operator waits for the Upgrader's answer before retrying as planned."""
    try:
        return float(os.environ.get("UPGRADER_HELP_WAIT_S") or 5)
    except ValueError:
        return 5.0


def _say(text: str, system: str | None = None, incident: str | None = None) -> None:
    log.info(text)
    think(AGENT, ROLE, text, incident=incident, system=system, kind="upgrade")


# ------------------------------------------------------------------ requesters (swarm side calls these)
def hand_off_repeat(monitor_id: str, sig: str, group: dict) -> dict | None:
    """Monitor #2: file a 'repeat' request once per problem signature. Returns the new doc, or None."""
    rid = f"GR-{sig}"
    res = col("gardener_requests").update_one({"_id": rid}, {
        "$setOnInsert": {"kind": "repeat", "from": monitor_id, "status": "open", "ts": now(), "answer": None,
                         "note": sig},
        "$set": {"system": group.get("system"), "problem": group.get("problem"),
                 "fault_class": group.get("fault_class"), "count": group.get("count"),
                 "evidence": list(group.get("evidence") or [])[-10:], "updated_at": now()}}, upsert=True)
    return col("gardener_requests").find_one({"_id": rid}) if res.upserted_id is not None else None


def ask(agent_id: str, incident: str, step: dict, evidence: list[str], fault_class: str | None) -> tuple[dict, bool]:
    """Operator: post (idempotently) a help request for this step. Returns (request, newly_posted)."""
    rid = f"GH-{step['step_id']}"  # step ids already carry the incident (INC-0001-s1)
    res = col("gardener_requests").update_one({"_id": rid}, {"$setOnInsert": {
        "kind": "help", "from": agent_id, "status": "open", "ts": now(), "answer": None,
        "incident": incident, "step_id": step["step_id"], "system": step["system"], "op": step["op"],
        "params": step.get("params") or {}, "attempts": list(step.get("attempts") or []),
        "evidence": evidence, "fault_class": fault_class}}, upsert=True)
    return col("gardener_requests").find_one({"_id": rid}), res.upserted_id is not None


def await_answer(rid: str, timeout: float, nap=time.sleep, check=None) -> dict | None:
    """Poll for the answer (requests are rare; a change stream per step isn't worth it)."""
    deadline = time.monotonic() + timeout
    while True:
        doc = col("gardener_requests").find_one({"_id": rid}, {"status": 1, "answer": 1})
        if doc and doc.get("status") == "answered":
            return doc.get("answer")
        if time.monotonic() >= deadline:
            return None
        if check:
            check()
        nap(0.25)


# ------------------------------------------------------------------ the Upgrader's answers
def _version(system: str) -> int | None:
    d = col("systems").find_one({"_id": system}, {"version": 1}) or {}
    return d.get("version")


def _remediation(system: str | None, fault_class: str | None):
    from . import remediations  # noqa: PLC0415
    rem = remediations.for_request(system or "")
    if rem is None or not fault_class or fault_class not in getattr(rem, "FAULT_CLASSES", {fault_class}):
        return None
    return rem


def answer_repeat(req: dict) -> dict:
    """Note the repeat; start (or re-queue) the vetted upgrade when a remediation covers it."""
    sys, fc, n = req.get("system"), req.get("fault_class"), req.get("count")
    col("gardener_notes").update_one({"_id": req.get("note")}, {"$set": {
        "upgrader_ack": {"request": req["_id"], "at": now()}}})
    rem = _remediation(sys, fc)
    if rem is None:
        return _tool_upgrade(req)
    existing = col("upgrade_requests").find_one({"system": sys, "fault_class": fc})
    if existing is None:
        uid = next_id("UPG")
        col("upgrade_requests").update_one({"system": sys, "fault_class": fc}, {"$setOnInsert": {
            "_id": uid, "system": sys, "weakness": f"{fc} on {sys} repeated {n}x (Monitor #2)",
            "fault_class": fc, "evidence": req.get("evidence") or [], "status": "requested", "diff": None,
            "tests": None, "approval_id": None, "version_before": _version(sys), "version_after": None,
            "prove": None, "requested_by": f"{AGENT} (from {req.get('from')})", "upgrader_request": req["_id"],
            "ts": now()}}, upsert=True)
        existing = col("upgrade_requests").find_one({"system": sys, "fault_class": fc})
        if existing["_id"] == uid:
            return {"kind": "upgrade", "upgrade": uid, "remediation": rem.NAME,
                    "text": f"noted; started {uid} ({rem.NAME}): sandbox test + replay proof, then approval, then apply"}
    if existing["status"] == "rejected":
        from . import pipeline  # noqa: PLC0415
        try:
            pipeline.requeue(existing["_id"])
        except pipeline.StepError:
            pass
        else:
            return {"kind": "upgrade", "upgrade": existing["_id"], "remediation": rem.NAME,
                    "text": f"noted; {existing['_id']} was rejected ({existing.get('reason') or 'no reason'}), "
                            f"re-queued it for another vetted run"}
    st = col("upgrade_requests").find_one({"_id": existing["_id"]}, {"status": 1})["status"]
    return {"kind": "upgrade", "upgrade": existing["_id"], "remediation": rem.NAME,
            "text": f"noted; {existing['_id']} already covers it (status {st})"}


def _tool_upgrade(req: dict) -> dict:
    """No code remediation covers the repeat: ship an accepted toolsmith tool through the upgrade pipeline."""
    from . import tools  # noqa: PLC0415
    sys, fc, n = req.get("system"), req.get("fault_class"), req.get("count")
    tool = tools.accepted(sys, fc)
    if tool is None:
        return {"kind": "none", "upgrade": None,
                "text": f"noted; no vetted code remediation or accepted tool covers {req.get('problem') or fc} "
                        f"on {sys} yet"}
    existing = col("upgrade_requests").find_one({"system": sys, "fault_class": fc, "tool_id": tool["_id"]})
    if existing is None:
        uid = next_id("UPG")
        col("upgrade_requests").update_one({"system": sys, "fault_class": fc, "tool_id": tool["_id"]}, {
            "$setOnInsert": {
                "_id": uid, "system": sys, "weakness": f"{fc} on {sys} repeated {n}x (Monitor #2)",
                "fault_class": fc, "remediation_kind": "tool", "tool_id": tool["_id"],
                "evidence": req.get("evidence") or [], "status": "requested", "diff": None, "tests": None,
                "approval_id": None, "version_before": _version(sys), "version_after": None, "prove": None,
                "requested_by": f"{AGENT} (from {req.get('from')})", "upgrader_request": req["_id"],
                "ts": now()}}, upsert=True)
        existing = col("upgrade_requests").find_one({"system": sys, "fault_class": fc, "tool_id": tool["_id"]})
        if existing["_id"] == uid:
            return {"kind": "upgrade", "upgrade": uid, "remediation": f"tool:{tool['name']}",
                    "text": f"noted; no code fix for {fc}, so started {uid} with tool {tool['name']}: "
                            f"sandbox replay, then approval, then adopt it as the standing recovery"}
    if existing["status"] == "rejected":
        from . import pipeline  # noqa: PLC0415
        try:
            pipeline.requeue(existing["_id"])
        except pipeline.StepError:
            pass
        else:
            return {"kind": "upgrade", "upgrade": existing["_id"], "remediation": f"tool:{tool['name']}",
                    "text": f"noted; re-queued {existing['_id']} (tool {tool['name']}) after "
                            f"{existing.get('reason') or 'a rejection'}"}
    return {"kind": "upgrade", "upgrade": existing["_id"], "remediation": f"tool:{tool['name']}",
            "text": f"noted; {existing['_id']} (tool {tool['name']}) already covers it (status {existing['status']})"}


def answer_help(req: dict) -> dict:
    """What the Upgrader remembers for this system + op. advice: params (try these), fast (retry at once)."""
    sys, op, params = req["system"], req["op"], req.get("params") or {}
    ver = _version(sys)
    # 1. a verified, current playbook that ran the same op on the same system with different params
    for pb in col("playbooks").find({"system": sys, "stale": False, "fix.op": op}).sort("verified_at", -1).limit(5):
        if ver is not None and pb.get("system_version") not in (None, ver):
            continue
        for f in pb.get("fix") or []:
            if f.get("system") == sys and f.get("op") == op and f.get("params") and f["params"] != params:
                return {"kind": "playbook", "ref": pb["_id"], "advice": {"params": f["params"]},
                        "text": f"playbook {pb['_id']} (from {pb.get('incident')}) verified {op} on {sys} v"
                                f"{pb.get('system_version')} with {f['params']}; try those params"}
    # 2. failure memory: this system failed this op before (FAILURE_MEMORY lessons, current version only)
    for ls in col("lessons").find({"system": sys, "op": op, "stale": {"$ne": True}}).sort("last_seen", -1).limit(3):
        if ver is not None and ls.get("system_version") not in (None, ver):
            continue
        fast = ls.get("kind") == "lied"
        return {"kind": "lesson", "ref": ls["_id"], "advice": {"fast": fast},
                "text": f"lesson {ls['_id']} (seen {ls.get('count', 1)}x): {(ls.get('why') or '')[:90]}"
                        + ("; don't wait on its ACK, retry at once and let the Verifier read back" if fast else "")}
    # 3. the evidence itself matches a vetted runbook: a system that ACKs without applying
    ev = " ".join(req.get("evidence") or [])
    lies = ev.count("ACK ≠ APPLIED")
    if lies >= 2:
        return {"kind": "runbook", "ref": "lying-ack", "advice": {"fast": True},
                "text": f"{sys} ACKed {op} {lies}x without applying it: a lying ACK, not a wrong fix. "
                        f"Resend the same op at once under a new action id; only the Verifier's read-back counts"}
    # 4. an accepted toolsmith tool that runs this op on this system (vetted params; the Verifier still checks)
    from . import tools  # noqa: PLC0415
    tool = tools.for_op(sys, op)
    if tool is not None:
        tparams = next((o.get("params") for o in tool["ops"] if o.get("system") == sys and o.get("op") == op), None)
        advice = {"params": tparams} if tparams and tparams != params and "$" not in str(tparams) else {}
        return {"kind": "tool", "ref": tool["_id"], "advice": advice,
                "text": f"tool {tool['name']} (vetted, {tool.get('successes', 0)} verified runs) does {op} on {sys}"
                        + (f" with {tparams}; use those params" if advice else "; same params as your step, "
                           "retry as planned and let the Verifier read back")}
    # 5. a proven upgrade / vetted remediation for this system (context, nothing to change in the step)
    upg = col("upgrade_requests").find_one({"system": sys, "status": "proven", "reverted_at": {"$exists": False}})
    if upg:
        return {"kind": "upgrade", "ref": upg["_id"], "advice": {},
                "text": f"{upg['_id']} already patched {sys} (v{upg.get('version_before')} -> "
                        f"v{upg.get('version_after')}); nothing to change in the step, retry as planned"}
    rem = _remediation(sys, req.get("fault_class"))
    if rem is not None:
        return {"kind": "remediation", "ref": rem.NAME, "advice": {},
                "text": f"vetted remediation {rem.NAME} covers {req.get('fault_class')} on {sys}; it ships through "
                        f"the upgrade pipeline, not this step. Retry as planned"}
    return {"kind": "none", "ref": None, "advice": {}, "text": f"none: nothing remembered for {op} on {sys}"}


def handle(req: dict) -> dict | None:
    """Claim one open request, answer it, narrate. Returns the answered doc (None if someone else claimed it)."""
    doc = col("gardener_requests").find_one_and_update(
        {"_id": req["_id"], "status": "open"}, {"$set": {"status": "answering", "claimed_at": now()}},
        return_document=ReturnDocument.AFTER)
    if doc is None:
        return None
    try:
        if doc["kind"] == "tool":
            from . import tools  # noqa: PLC0415
            ans = tools.receive(doc)
        else:
            ans = answer_repeat(doc) if doc["kind"] == "repeat" else answer_help(doc)
    except Exception as e:  # never leave an Operator waiting on a crashed answer
        log.exception("upgrader request %s failed", doc["_id"])
        ans = {"kind": "none", "advice": {}, "text": f"none (upgrader error: {type(e).__name__})"}
    col("gardener_requests").update_one({"_id": doc["_id"]}, {"$set": {
        "status": "answered", "answer": ans, "answered_at": now()}})
    if doc["kind"] == "tool":
        _say(f"Upgrader ← {doc.get('from')}: {ans['text']}.", doc.get("system"))
    elif doc["kind"] == "repeat":
        _say(f"Upgrader ← Monitor #2: {doc.get('problem')} repeated {doc.get('count')}× — {ans['text']}.",
             doc.get("system"))
    else:
        _say(f"Upgrader → {doc.get('from')}: {ans['text']}.", doc.get("system"), doc.get("incident"))
    return col("gardener_requests").find_one({"_id": doc["_id"]})


def serve(stop: threading.Event) -> None:
    """Thread inside upgrader.main: answer open requests (change stream wake-up + polling fallback)."""
    stream = None
    while not stop.is_set():
        try:
            if stream is None:
                try:
                    stream = col("gardener_requests").watch([{"$match": {"operationType": "insert"}}],
                                                            max_await_time_ms=int(POLL_S * 1000))
                except Exception:
                    stream = None
            for req in list(col("gardener_requests").find({"status": "open"}).sort("ts", 1).limit(20)):
                handle(req)
            # stale claims (an Upgrader killed mid-answer) go back to open
            col("gardener_requests").update_many(
                {"status": "answering", "claimed_at": {"$lt": now() - _td(30)}}, {"$set": {"status": "open"}})
            if stream is not None:
                stream.try_next()
            else:
                stop.wait(POLL_S)
        except Exception as e:
            log.warning("help desk error: %s", e)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            stream = None
            stop.wait(POLL_S)
    if stream is not None:
        stream.close()


def _td(s: float):
    from datetime import timedelta  # noqa: PLC0415
    return timedelta(seconds=s)
