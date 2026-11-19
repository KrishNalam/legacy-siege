"""Upgrade steps for remediation_kind "tool": ship an accepted platform tool instead of a code diff.

upgrader.pipeline dispatches here once a request carries remediation_kind "tool" (set by the help desk
for a Sentinel #2 repeat, or by pipeline._pick_kind when no code remediation covers the problem but the
Gardener has accepted a tool for it). Same statuses, same atomic transitions, same Beekeeper gate:

    plan   re-vet the tool (active, exact whitelist, current system version); the "diff" is its op list
    test   sandbox replay on an isolated copy of the estate: fault active -> tool ops -> fault resolved
    gate   (pipeline.request_approval) approval text from approval_text()
    deploy adopt the tool as the swarm's standing recovery: tools.gardener.adopted + ledger receipt, one txn
    prove  fire the fault on the estate; proven when the swarm resolves that incident with the tool's steps
           and the Guard verified every one of them (the Gardener never sends ops to the estate itself)
"""
from __future__ import annotations

import json
import os
import time

from common.db import col, now, txn

from . import testing, tools
from . import pipeline as P

PROVE_WINDOW_S = float(os.environ.get("GARDENER_TOOL_PROVE_S") or 120)


def _tool(upg: dict) -> dict:
    tool = col("tools").find_one({"_id": upg.get("tool_id")})
    why = tools.vet(tool)
    if why:
        P.reject(upg["_id"], f"tool {upg.get('tool_id')}: {why}", upg.get("system"))
    return tool


def _ops_text(ops: list[dict]) -> str:
    return "\n".join(f"{i}. {o['system']}.{o['op']} {json.dumps(o.get('params') or {}, sort_keys=True)}"
                     + ("  [needs approval]" if o.get("destructive") else "") for i, o in enumerate(ops, 1))


def plan(upg: dict) -> dict:
    upg_id, system = upg["_id"], upg["system"]
    tool = _tool(upg)
    text = _ops_text(tool["ops"])
    ev = P.evidence_summary(list(upg.get("evidence") or []))
    explanation = (f"No code fix covers {upg.get('fault_class')} on {system}; platform tool {tool['name']} "
                   f"({tool.get('source')}, built from {len(tool.get('built_from') or [])} repeated failures) runs "
                   f"{len(tool['ops'])} whitelisted ops. Adopting it makes it the swarm's standing recovery; "
                   f"Workers still send each op and the Guard still reads every one back.")
    doc = P.advance(upg_id, {"planning"}, "planning",
                    diff=text, diff_sha256=P.patcher.sha256(json.dumps(tool["ops"], sort_keys=True)),
                    diff_lines=len(tool["ops"]), file=None, remediation=f"tool:{tool['name']}",
                    layout={"remediation": f"tool:{tool['name']}", "function": None, "source": "tool registry",
                            "prove_fault": upg.get("fault_class")},
                    tool_name=tool["name"], tool_version=tool.get("version"), tool_ops=tool["ops"],
                    explanation=explanation, evidence_summary=ev, planned_at=now())
    P.say(f"{upg_id}: no code fix for {upg.get('fault_class')} on {system}; planning with platform tool "
          f"{tool['name']} ({len(tool['ops'])} vetted ops)", system)
    return doc


def test(upg: dict) -> dict:
    upg_id, system, fc = upg["_id"], upg["system"], upg.get("fault_class")
    tool = _tool(upg)
    if P.patcher.sha256(json.dumps(tool["ops"], sort_keys=True)) != upg.get("diff_sha256"):
        P.reject(upg_id, "tool ops changed since the plan was made; re-plan required", system)
    P.say(f"{upg_id}: sandbox replay - fire {fc} on a copy of the estate, run tool {tool['name']}, read back",
          system)
    res = testing.run_tool_tests(fc, system, tool["ops"], P.SYSTEMS_DIR)
    tests = {k: res.get(k) for k in ("passed", "output", "pytest_passed", "replay_passed", "sandbox")}
    if not res["passed"]:
        P.reject(upg_id, f"tests failed ({res['summary']})", system, tests=tests)
    doc = P.advance(upg_id, {"testing"}, "testing", tests=tests, tested_at=now())
    P.say(f"{upg_id}: sandbox green - tool {tool['name']} cleared {fc} on {system} and it read back healthy",
          system)
    return doc


def approval_text(upg: dict) -> tuple[str, str]:
    desc = (f"{upg['_id']}: adopt platform tool {upg.get('tool_name')} as the standing recovery for "
            f"{upg.get('fault_class')} on {upg['system']} - {upg.get('explanation', '')}")[:600]
    risk = (f"low: no code change; {upg.get('diff_lines')} whitelisted ops, sandbox replay passed; destructive "
            f"ops still need their own approval and the Guard verifies every step")
    return desc, risk


def deploy(upg: dict, apr: dict) -> dict:
    upg_id, system = upg["_id"], upg["system"]
    tool = _tool(upg)
    ts = now()
    receipt = {
        "action_id": f"{upg_id}-deploy", "step_id": None, "incident": upg_id, "upgrade": upg_id,
        "task_id": None, "agent": P.AGENT, "system": system, "op": "adopt_tool",
        "params": {"tool_id": tool["_id"], "remediation": upg.get("remediation"), "diff_sha256": upg["diff_sha256"]},
        "status": "verified", "system_response": {"tool_status": tool["status"]},
        "evidence": f"tool {tool['_id']} active and vetted; adopted as standing recovery for {upg.get('fault_class')}",
        "approver": apr.get("decided_by"), "approval_id": apr["_id"], "evidence_incidents": upg.get("evidence"),
        "tests_passed": (upg.get("tests") or {}).get("passed"), "version_before": upg.get("version_before"),
        "version_after": upg.get("version_before"), "ts": ts, "updated_at": ts,
    }

    def record(s):
        r = col("tools").update_one({"_id": tool["_id"], "status": "active"}, {"$set": {
            "gardener.adopted": upg_id, "gardener.adopted_at": ts}}, session=s)
        if r.matched_count != 1:
            raise P.StepError(f"{upg_id}: tool {tool['_id']} retired while deploying")
        col("ledger").update_one({"action_id": receipt["action_id"]}, {"$setOnInsert": receipt},
                                 upsert=True, session=s)
        col("upgrade_requests").update_one(
            {"_id": upg_id, "status": "deploying"},
            {"$set": {"status": "deployed", "version_after": upg.get("version_before"), "deployed_at": ts,
                      "approver": apr.get("decided_by"), "updated_at": ts}}, session=s)

    try:
        txn(record)
    except P.StepError as e:
        P.reject(upg_id, str(e), system)
    P.set_phase("deployed", upg_id)
    P.say(f"{upg_id}: deployed - tool {tool['name']} is now the standing recovery for {upg.get('fault_class')}",
          system)
    return P.get(upg_id)


def _judge(inc: dict, tool_id: str) -> tuple[bool, str]:
    steps = [s for s in inc.get("steps") or [] if s.get("tool_id") == tool_id]
    if not steps:
        return False, f"{inc['_id']} was fixed without the tool"
    bad = [s["step_id"] for s in steps if s.get("status") != "verified"]
    if bad:
        return False, f"{inc['_id']}: tool steps not Guard-verified: {', '.join(bad)}"
    return True, f"{inc['_id']}: {len(steps)} tool steps, all Guard-verified"


def prove(upg: dict, window_s: float = PROVE_WINDOW_S, poll_s: float = 2.0) -> dict:
    upg_id, system, fc = upg["_id"], upg["system"], upg.get("fault_class")
    P.set_phase("proving", upg_id)
    fired_at = now()
    try:
        via = P.fire_fault(fc, system)
    except Exception as e:
        col("upgrade_requests").update_one({"_id": upg_id}, {"$set": {"prove": {
            "fault": fc, "fired_at": fired_at, "window_s": window_s, "incident_opened": None, "error": str(e)}}})
        P.say(f"{upg_id}: could not replay {fc} ({e})", system)
        P.set_phase(None)
        return P.get(upg_id)
    base = {"fault": fc, "fired_at": fired_at, "window_s": window_s, "via": via, "tool_id": upg["tool_id"]}
    col("upgrade_requests").update_one({"_id": upg_id}, {"$set": {"prove": {**base, "incident_opened": None}}})
    P.say(f"{upg_id}: replayed {fc} on {system}; watching {int(window_s)}s for the swarm to fix it with the tool",
          system)
    verdict, why, inc_id = False, f"no {fc} incident resolved within {int(window_s)}s", None
    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        inc = col("incidents").find_one({"opened_at": {"$gte": fired_at}, "fault_class": fc, "status": "resolved"},
                                        sort=[("opened_at", 1)])
        if inc:
            inc_id = inc["_id"]
            verdict, why = _judge(inc, upg["tool_id"])
            break
        time.sleep(poll_s)
    prove_doc = {**base, "incident_opened": inc_id is not None, "incident": inc_id, "verdict": why}
    if verdict:
        P.advance(upg_id, {"deployed"}, "proven", prove=prove_doc, proven_at=now())
        P.say(f"{upg_id}: proven - {why}", system)
    else:
        col("upgrade_requests").update_one({"_id": upg_id}, {"$set": {"prove": prove_doc}})
        P.say(f"{upg_id}: not proven yet ({why}); upgrade stays 'deployed'", system)
    P.set_phase(None)
    return P.get(upg_id)
