"""The Gardener's toolbox: vetted platform-engineer tools it accepts and may ship as an upgrade step.

Platform engineers (W20, PLATFORM_ENGINEERS=1) publish each tool into `swarm.gardener_requests` as
kind "tool" (envelope in results/W20-delivery-interface.md: payload {tool_id, name, version, status,
ops, preconditions, source, built_from}). A delivered copy never authorises anything: the Gardener
re-reads `swarm.tools`, insists the tool is still active, its ops are exactly the vetted whitelist for
that fault class (no op, parameter, order or approval flag changed) and its system version is the
current one. Then it records its acceptance on the tool (`tools.gardener`), or drops it when the
tool is retired.

An accepted tool can be one *step* of an upgrade (remediation_kind "tool", upgrader.pipeline):
    plan   -> the tool's ops, re-checked against the whitelist
    test   -> sandbox replay: fire the fault on an isolated copy of the estate, run the tool's ops,
              the system must read back healthy and the fault resolved (upgrader.replay --tool)
    gate   -> Beekeeper approval, same as a code diff
    deploy -> adopt the tool as the swarm's standing recovery for that fault (ledger receipt, one txn)
    prove  -> fire the fault again on the estate; proven when the swarm resolves the incident with
              the tool's steps and the Guard verified every one of them.
The Gardener never runs ops against the live estate itself; Workers do, and the Guard checks each.
"""
from __future__ import annotations

from common.db import col, now

# The tools W20 can build (fault class -> system). Mirrors agents.platform.SUPPORTED.
SUPPORTED = {"batch_abort": "mainframe", "disk_fill": "unix", "cron_stop": "unix"}
SAMPLE = {"poison_records": ["$record_id"]}


def whitelist(fault: str) -> list[dict]:
    """The vetted op template for a fault class (identical to agents.platform.template)."""
    from agents import policy  # noqa: PLC0415

    return policy.fix_plan(fault, SAMPLE)


def version(system: str) -> int | None:
    return (col("systems").find_one({"_id": system}, {"version": 1}) or {}).get("version")


def vet(tool: dict | None) -> str | None:
    """None if the tool may be used now, else the reason it may not."""
    if not tool:
        return "not in the tool registry"
    if tool.get("status") != "active":
        return f"tool is {tool.get('status')}"
    fault = tool.get("fault_class")
    if fault not in SUPPORTED:
        return f"no vetted whitelist for fault class {fault!r}"
    pre = tool.get("preconditions") or {}
    if pre.get("system") != SUPPORTED[fault]:
        return f"precondition system {pre.get('system')!r} does not match {SUPPORTED[fault]!r}"
    if tool.get("ops") != whitelist(fault):
        return "ops differ from the vetted whitelist"
    ver = version(SUPPORTED[fault])
    if ver is not None and pre.get("system_version") not in (None, ver):
        return f"built for {pre.get('system')} v{pre.get('system_version')}, estate is v{ver}"
    return None


def receive(req: dict) -> dict:
    """Answer a kind 'tool' request: accept (record on the tool) or drop/refuse it."""
    p = req.get("payload") or {}
    tid = p.get("tool_id")
    tool = col("tools").find_one({"_id": tid}) if tid else None
    name = p.get("name") or tid
    if p.get("status") == "retired" or (tool and tool.get("status") == "retired"):
        col("tools").update_one({"_id": tid}, {"$set": {"gardener.accepted": False, "gardener.dropped_at": now(),
                                                         "gardener.request": req["_id"]}})
        return {"kind": "tool", "ref": tid, "advice": {},
                "text": f"dropped tool {name}: retired by its platform engineer; no upgrade will use it"}
    why = vet(tool)
    if why:
        if tool:
            col("tools").update_one({"_id": tid}, {"$set": {"gardener.accepted": False, "gardener.reason": why,
                                                             "gardener.request": req["_id"], "gardener.at": now()}})
        return {"kind": "none", "ref": tid, "advice": {}, "text": f"refused tool {name}: {why}"}
    col("tools").update_one({"_id": tid}, {"$set": {"gardener.accepted": True, "gardener.at": now(),
                                                     "gardener.request": req["_id"], "gardener.reason": None}})
    return {"kind": "tool", "ref": tid, "advice": {},
            "text": f"accepted tool {name} ({len(tool['ops'])} vetted ops for {tool['fault_class']} on "
                    f"{tool['preconditions']['system']}); it can now be a step in an upgrade"}


def accepted(system: str | None, fault_class: str | None) -> dict | None:
    """The newest tool the Gardener accepted for this problem that still passes vetting."""
    if not system or not fault_class:
        return None
    for tool in col("tools").find({"gardener.accepted": True, "fault_class": fault_class,
                                   "preconditions.system": system}).sort("version", -1).limit(5):
        if vet(tool) is None:
            return tool
    return None


def for_op(system: str, op: str) -> dict | None:
    """An accepted, still-valid tool that contains this op on this system (for a Worker's help request)."""
    for tool in col("tools").find({"gardener.accepted": True, "preconditions.system": system,
                                   "ops.op": op}).limit(5):
        if vet(tool) is None:
            return tool
    return None
