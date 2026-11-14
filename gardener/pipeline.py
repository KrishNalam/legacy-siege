"""Gardener pipeline: plan -> test -> gate -> deploy -> prove (+ revert for demo resets).

Every step is a plain function taking an upgrade id, reading the upgrade request from
MongoDB and writing its result back, so any process (gardener.main, the war room, a
test) can drive any step and a crashed Gardener resumes from the stored status.

    swarm.upgrade_requests.status:
      requested -> planning -> testing -> awaiting_approval -> deploying -> deployed -> proven
                     \\___________\\______________\\________________\\-> rejected (with `reason`)

MongoDB features used on purpose:
  * conditional findOneAndUpdate for every status transition (no double-advance, no races);
  * a multi-document transaction to create the approval + flip the upgrade to
    awaiting_approval, and again to mark it deployed + write its ledger receipt;
  * change streams to wait for the Beekeeper's decision and to watch for incidents
    during the prove window (each with a polling fallback);
  * an aggregation pipeline that summarises the evidence incidents.

Guardrails: writes only inside SYSTEMS_DIR; one function body per upgrade; diffs over
MAX_DIFF_LINES are refused; the original file is backed up before deploy; deploy
re-derives the patch and refuses unless it is byte-identical to what was approved.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from pymongo import ReturnDocument

from common import config, pause
from common.db import col, next_id, now, txn
from common.events import think
from common.llm import complete

from . import patcher, remediations, testing
from .patcher import PatchError

log = logging.getLogger("gardener")

AGENT, ROLE = "gardener-1", "gardener"
SYSTEMS_DIR = config.ROOT / "systems"
BACKUP_DIR = Path(__file__).resolve().parent / "backups"
PROVE_FAULT = "batch_abort"
PROVE_WINDOW_S = config.PROVE_WINDOW_S  # env PROVE_WINDOW_S (default 30)
WATCHED_SYSTEMS = ["mainframe", "unix", "portal"]
TERMINAL = {"proven", "rejected"}
PAUSE_MIN_S = float(os.environ.get("GARDENER_PAUSE_MIN_S") or 4)  # long enough to see on screen


class Rejected(Exception):
    """The upgrade was moved to status 'rejected' (reason stored on the doc)."""


class StepError(RuntimeError):
    """A step was called on an upgrade in the wrong status (nothing was changed)."""


class Abandoned(StepError):
    """The approval (or the upgrade request) we were waiting on vanished, e.g. a demo reset wiped it."""


# ------------------------------------------------------------------ small helpers
def say(text: str, system: str | None = None) -> None:
    """One short sentence on the activity stream (swarm.thoughts, kind 'upgrade')."""
    log.info(text)
    think(AGENT, ROLE, text, system=system, kind="upgrade")


def set_phase(phase: str | None, upgrade: str | None = None, target: str = "workshop") -> None:
    """Mirror progress onto the Gardener's agent doc (no-op if gardener.main isn't registered)."""
    col("agents").update_one({"_id": AGENT}, {"$set": {
        "phase": phase, "upgrade": upgrade, "target": target,
        "status": "working" if phase else "idle", "last_seen": now()}})


def get(upg_id: str) -> dict:
    doc = col("upgrade_requests").find_one({"_id": upg_id})
    if not doc:
        raise StepError(f"{upg_id} not found")
    return doc


def advance(upg_id: str, from_status: set[str], to_status: str, **fields) -> dict:
    """Atomic, conditional status transition. Raises StepError if the doc isn't in from_status."""
    doc = col("upgrade_requests").find_one_and_update(
        {"_id": upg_id, "status": {"$in": sorted(from_status)}},
        {"$set": {"status": to_status, "updated_at": now(), **fields}},
        return_document=ReturnDocument.AFTER)
    if doc is None:
        cur = col("upgrade_requests").find_one({"_id": upg_id}, {"status": 1})
        raise StepError(f"{upg_id}: expected status in {sorted(from_status)}, found {cur and cur.get('status')}")
    return doc


def reject(upg_id: str, reason: str, system: str | None = None, **fields) -> None:
    col("upgrade_requests").update_one(
        {"_id": upg_id, "status": {"$nin": sorted(TERMINAL)}},
        {"$set": {"status": "rejected", "reason": reason, "updated_at": now(), **fields}})
    say(f"{upg_id} rejected: {reason}", system)
    set_phase(None)
    raise Rejected(reason)


def inside_systems(path: Path) -> Path:
    """Guardrail: resolve `path` and refuse anything outside SYSTEMS_DIR."""
    p, root = path.resolve(), SYSTEMS_DIR.resolve()
    if root not in p.parents:
        raise PatchError(f"refusing to touch {p}: outside {root}")
    return p


def http_post(path: str, json: dict | None = None, timeout: float = 15) -> dict:
    """POST to the systems API (patched in tests)."""
    r = httpx.post(f"{config.SYSTEMS_URL}{path}", json=json or {}, timeout=timeout)
    r.raise_for_status()
    return r.json() if r.content else {}


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.gardener.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)  # atomic on POSIX: the reloader never sees a half-written file


# ------------------------------------------------------------------ the patch itself
@dataclass
class Patch:
    system: str
    path: Path
    original: str
    patched: str
    diff: str
    layout: dict

    @property
    def rel(self) -> str:
        return f"systems/{self.path.name}"


def build_patch(system: str, original: str | None = None, fault_class: str | None = None) -> Patch:
    """Deterministically derive the remediation patch for `system` (raises PatchError)."""
    rem = remediations.for_request(system, fault_class)
    if rem is None:
        raise PatchError(f"no predetermined remediation for system {system!r}")
    path = inside_systems(SYSTEMS_DIR / f"{system}.py")
    if original is None:
        if not path.exists():
            raise PatchError(f"{path.name} does not exist")
        original = path.read_text()
    tree = patcher.parse(original)
    site = patcher.locate_function(original, rem.FUNCTION, tree)
    if rem.already_applied(site.docstring):
        raise PatchError(f"{rem.FUNCTION}() already carries the {rem.NAME} remediation")
    layout = rem.detect_layout(tree)
    patched = patcher.replace_body(original, site, rem.render_body(site.arg_name, layout))
    if patcher.changed_outside(original, patched, site):  # self-check: body-only edit
        raise PatchError("internal: patch touched lines outside the function body")
    diff = patcher.unified_diff(original, patched, f"systems/{path.name}")
    n = patcher.diff_line_count(diff)
    if n > patcher.MAX_DIFF_LINES:
        raise PatchError(f"diff is {n} lines (> {patcher.MAX_DIFF_LINES}); refusing")
    return Patch(system, path, original, patched, diff, {
        "source": layout.source, "length": layout.length_expr, "fields": layout.field_expr,
        "notes": layout.notes, "remediation": rem.NAME, "function": rem.FUNCTION,
        "prove_fault": getattr(rem, "PROVE_FAULT", PROVE_FAULT)})


def _approved_patch(upg: dict) -> Patch:
    """Re-derive the patch and insist it is exactly the one planned (and approved).

    If a crashed deploy already wrote the file, the original comes from the recorded backup.
    """
    path = inside_systems(SYSTEMS_DIR / f"{upg['system']}.py")
    current = path.read_text() if path.exists() else ""
    if patcher.sha256(current) == upg.get("source_sha256"):
        p = build_patch(upg["system"], current, upg.get("fault_class"))
    elif upg.get("backup") and Path(upg["backup"]).exists() \
            and patcher.sha256(Path(upg["backup"]).read_text()) == upg.get("source_sha256"):
        p = build_patch(upg["system"], Path(upg["backup"]).read_text(), upg.get("fault_class"))
    else:
        raise PatchError(f"{path.name} changed since the plan was made; re-plan required")
    if patcher.sha256(p.diff) != upg.get("diff_sha256"):
        raise PatchError("re-derived diff differs from the planned diff")
    return p


# ------------------------------------------------------------------ evidence + explanation
def evidence_summary(incident_ids: list[str]) -> dict:
    """Aggregate the evidence incidents: root causes, poison record ids, time spent fixing."""
    rows = list(col("incidents").aggregate([
        {"$match": {"_id": {"$in": incident_ids}}},
        {"$project": {
            "root_cause_system": 1, "root_cause": 1, "fault_class": 1, "opened_at": 1,
            "ttr_s": "$metrics.ttr_s",
            "records": {"$map": {
                "input": {"$filter": {"input": {"$ifNull": ["$steps", []]}, "as": "s",
                                      "cond": {"$eq": ["$$s.op", "quarantine_record"]}}},
                "as": "s", "in": "$$s.params.record_id"}},
        }},
        {"$sort": {"opened_at": 1}},
    ]))
    return {
        "incidents": [r["_id"] for r in rows],
        "missing": sorted(set(incident_ids) - {r["_id"] for r in rows}),
        "root_causes": sorted({r.get("root_cause") for r in rows if r.get("root_cause")}),
        "poison_records": [rec for r in rows for rec in (r.get("records") or []) if rec],
        "total_ttr_s": round(sum((r.get("ttr_s") or 0) for r in rows), 1),
    }


def explain(upg: dict, patch: Patch, ev: dict) -> str:
    """1-2 sentence explanation. The LLM may phrase it; the code change never comes from it."""
    ids = ", ".join(ev["incidents"] or upg.get("evidence") or []) or "past incidents"
    fallback = (f"{patch.system}.validate_record() accepted every record, so a malformed amount reached "
                f"NIGHTBAT and ABENDed it (S0C7) in {ids}. The patch rejects records that are not "
                f"{patch.layout['length']} chars with a numeric account and signed numeric amount; "
                f"valid records are unchanged.")
    text = complete(
        "You explain a small, already-written code fix to a bank operator. Write at most two plain "
        "sentences, no code, no markdown.",
        f"Weakness: {upg.get('weakness')}\nEvidence incidents: {ids}\nRoot causes: {ev['root_causes']}\n"
        f"Diff:\n{patch.diff}", max_tokens=120)
    return (text or fallback)[:400]


# ------------------------------------------------------------------ 1. plan
def _pick_kind(upg: dict) -> dict:
    """GARDENER_HELP=1: no vetted code remediation for this problem but an accepted platform tool covers
    it -> the upgrade ships that tool instead (remediation_kind 'tool'). Otherwise unchanged."""
    if upg.get("remediation_kind") or not _help_enabled():
        return upg
    system, fc = upg.get("system"), upg.get("fault_class")
    if remediations.for_request(system or "", fc) is not None:
        return upg
    from . import tools  # noqa: PLC0415
    tool = tools.accepted(system, fc)
    if tool is None:
        return upg
    return advance(upg["_id"], {"planning"}, "planning", remediation_kind="tool", tool_id=tool["_id"])


def _help_enabled() -> bool:
    from . import help as help_desk  # noqa: PLC0415
    return help_desk.enabled()


def _is_tool(upg: dict) -> bool:
    return upg.get("remediation_kind") == "tool"


def plan(upg_id: str) -> dict:
    """Claim the request, read the source + evidence, derive the predetermined diff."""
    upg = advance(upg_id, {"requested", "planning"}, "planning", owner=AGENT)
    system = upg.get("system", "")
    set_phase("planning", upg_id)
    upg = _pick_kind(upg)
    if upg.get("remediation_kind") == "tool":
        from . import tool_upgrade  # noqa: PLC0415
        return tool_upgrade.plan(upg)
    ev_ids = ", ".join(upg.get("evidence") or [])
    say(f"{upg_id}: reading systems/{system}.py" + (f" and evidence {ev_ids}" if ev_ids else ""), system)
    try:
        patch = build_patch(system, fault_class=upg.get("fault_class"))
    except PatchError as e:
        reject(upg_id, str(e), system)
    ev = evidence_summary(list(upg.get("evidence") or []))
    explanation = explain(upg, patch, ev)
    doc = advance(upg_id, {"planning"}, "planning",
                  diff=patch.diff, diff_sha256=patcher.sha256(patch.diff),
                  diff_lines=patcher.diff_line_count(patch.diff),
                  source_sha256=patcher.sha256(patch.original), file=patch.rel,
                  remediation=patch.layout["remediation"], layout=patch.layout,
                  explanation=explanation, evidence_summary=ev, planned_at=now())
    say(f"{upg_id}: proposed a {doc['diff_lines']}-line diff to {patch.layout['function']}() "
        f"(layout from {patch.layout['source']})", system)
    return doc


# ------------------------------------------------------------------ 2. test
def test(upg_id: str) -> dict:
    """pytest systems/tests/test_<sys>.py + regression replay, both against a patched sandbox."""
    upg = advance(upg_id, {"planning", "testing"}, "testing")
    system = upg["system"]
    set_phase("testing", upg_id)
    if _is_tool(upg):
        from . import tool_upgrade  # noqa: PLC0415
        return tool_upgrade.test(upg)
    try:
        patch = _approved_patch(upg)
    except PatchError as e:
        reject(upg_id, str(e), system)
    say(f"{upg_id}: running test_{system}.py and replaying batch_abort in a sandbox", system)
    res = testing.run_tests(system, patch.patched, SYSTEMS_DIR)
    tests = {"passed": res["passed"], "output": res["output"],
             "pytest_passed": res["pytest_passed"], "replay_passed": res["replay_passed"], "sandbox": res.get("sandbox")}
    if not res["passed"]:
        reject(upg_id, f"tests failed ({res['summary']})", system, tests=tests)
    doc = advance(upg_id, {"testing"}, "testing", tests=tests, tested_at=now())
    say(f"{upg_id}: tests green, replayed fault no longer ABENDs the batch", system)
    return doc


# ------------------------------------------------------------------ 3. gate
def request_approval(upg_id: str) -> dict:
    """Create the Beekeeper approval and move to awaiting_approval in ONE transaction."""
    upg = get(upg_id)
    if upg["status"] == "awaiting_approval" and upg.get("approval_id"):
        return upg  # resumed: approval already exists
    if upg["status"] != "testing" or not (upg.get("tests") or {}).get("passed"):
        raise StepError(f"{upg_id}: gate needs status 'testing' with passing tests")
    apr_id = next_id("APR")
    if _is_tool(upg):
        from . import tool_upgrade  # noqa: PLC0415
        desc, risk = tool_upgrade.approval_text(upg)
    else:
        desc = (f"{upg_id}: patch {upg['file']} {upg['layout']['function']}() - "
                f"{upg.get('explanation', '')}"[:600])
        risk = (f"low: one function body, {upg['diff_lines']}-line diff, tests passed; "
                f"valid records unchanged, malformed records skipped instead of aborting the batch")
    approval = {
        "_id": apr_id, "kind": "upgrade", "incident": None, "step_id": None, "action_id": None,
        "upgrade": upg_id, "requested_by": AGENT,
        "desc": desc, "risk": risk,
        "diff": upg["diff"], "tests_passed": True,
        "status": "pending", "decided_by": None, "ts": now(), "decided_at": None,
    }
    def gate(s):
        col("approvals").insert_one(approval, session=s)
        res = col("upgrade_requests").update_one(
            {"_id": upg_id, "status": "testing"},
            {"$set": {"status": "awaiting_approval", "approval_id": apr_id, "updated_at": now()}}, session=s)
        if res.modified_count != 1:
            raise StepError(f"{upg_id} changed status while requesting approval")

    txn(gate)
    set_phase("awaiting_approval", upg_id)
    say(f"{upg_id}: diff and test results sent to the Beekeeper ({apr_id})", upg["system"])
    return get(upg_id)


def wait_for_decision(apr_id: str, timeout: float | None = None, poll_s: float = 3.0,
                      upg_id: str | None = None) -> dict | None:
    """Block until approvals/<apr_id> is approved or denied (change stream + polling fallback).

    Raises Abandoned if the approval disappears, or (with ``upg_id``) the upgrade request is gone or no
    longer awaiting this approval -- a reset wiped it, so the caller goes back to watching for new requests
    instead of waiting forever."""
    deadline = None if timeout is None else time.monotonic() + timeout
    approvals = col("approvals")

    def decided() -> dict | None:
        d = approvals.find_one({"_id": apr_id})
        if d is None:
            raise Abandoned(f"approval {apr_id} is gone (reset?)")
        if upg_id is not None and not col("upgrade_requests").find_one(
                {"_id": upg_id, "status": "awaiting_approval", "approval_id": apr_id}, {"_id": 1}):
            raise Abandoned(f"{upg_id} is gone or no longer awaiting {apr_id} (reset?)")
        return d if d.get("status") in ("approved", "denied") else None

    stream = None
    try:
        stream = approvals.watch([{"$match": {"documentKey._id": apr_id}}],
                                 full_document="updateLookup", max_await_time_ms=1000)
    except Exception as e:  # change streams unavailable -> polling only
        log.warning("approval change stream unavailable (%s); polling", e)
    try:
        last_poll = 0.0
        while deadline is None or time.monotonic() < deadline:
            if time.monotonic() - last_poll >= poll_s:  # also covers a decision made before watch()
                last_poll = time.monotonic()
                if d := decided():
                    return d
            if stream is not None:
                try:
                    ev = stream.try_next()
                except Exception as e:  # e.g. the collection was dropped by a reset: keep polling
                    log.warning("approval change stream ended (%s); polling", e)
                    stream.close()
                    stream, last_poll = None, 0.0
                    continue
                if (ev or {}).get("operationType") in ("delete", "drop", "dropDatabase", "invalidate"):
                    last_poll = 0.0  # re-check right away (raises Abandoned if it is really gone)
                    continue
                doc = (ev or {}).get("fullDocument") or {}
                if doc.get("status") in ("approved", "denied"):
                    return doc
            else:
                time.sleep(min(poll_s, 1.0))
        return decided()
    finally:
        if stream is not None:
            stream.close()


def gate(upg_id: str, wait: bool = True, timeout: float | None = None) -> str:
    """Request approval (idempotent) and optionally wait. Returns 'pending'|'approved'; denial rejects."""
    upg = request_approval(upg_id)
    if not wait:
        return "pending"
    d = wait_for_decision(upg["approval_id"], timeout, upg_id=upg_id)
    if d is None:
        return "pending"
    if d["status"] == "denied":
        reject(upg_id, f"denied by {d.get('decided_by') or 'Beekeeper'}", upg["system"])
    say(f"{upg_id}: approved by {d.get('decided_by') or 'Beekeeper'}", upg["system"])
    return "approved"


# ------------------------------------------------------------------ 4. deploy
def _backup(system: str, text: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUP_DIR / f"{system}.{now().strftime('%Y%m%dT%H%M%S%fZ')}.py"
    path.write_text(text)
    return path


def reload_system(system: str) -> dict:
    return http_post(f"/_admin/reload/{system}")


def deploy(upg_id: str) -> dict:
    """Write the approved patch into systems/, hot-reload it, record the ledger receipt."""
    upg = get(upg_id)
    system = upg["system"]
    apr = col("approvals").find_one({"_id": upg.get("approval_id")}) or {}
    if apr.get("status") != "approved":
        raise StepError(f"{upg_id}: approval {upg.get('approval_id')} is {apr.get('status')!r}, not approved")
    upg = advance(upg_id, {"awaiting_approval", "deploying"}, "deploying")
    set_phase("deploying", upg_id, target=system)
    if _is_tool(upg):
        from . import tool_upgrade  # noqa: PLC0415
        return tool_upgrade.deploy(upg, apr)
    try:
        patch = _approved_patch(upg)
    except PatchError as e:
        reject(upg_id, str(e), system)

    # GARDENER_PAUSE=1: every agent (swarm + Red fleet) holds still while the file is swapped and reloaded.
    # A lease, renewed while we work: if this process dies here, everyone resumes within the lease.
    with pause.hold(AGENT, system, upg_id, f"Gardener upgrading {system}", min_s=PAUSE_MIN_S) as paused:
        if paused:
            say(f"Gardener upgrading {system} — all agents paused", system)
        if patcher.sha256(patch.path.read_text()) != patcher.sha256(patch.patched):
            backup = Path(upg["backup"]) if upg.get("backup") else _backup(system, patch.original)
            col("upgrade_requests").update_one({"_id": upg_id}, {"$set": {"backup": str(backup)}})
            _write_atomic(patch.path, patch.patched)
            say(f"{upg_id}: wrote {patch.rel} (backup {backup.name}); hot-reloading", system)
        try:
            resp = reload_system(system)
        except Exception as e:  # reload failed: put the original back so the estate never runs unreviewed code
            _write_atomic(patch.path, patch.original)
            try:
                reload_system(system)
            except Exception:
                pass
            reject(upg_id, f"reload failed ({e}); original restored", system)
    if paused:
        say(f"{upg_id}: {system} upgraded — all agents resumed", system)
    vb, va = resp.get("version_before"), resp.get("version_after")
    ts = now()
    receipt = {
        "action_id": f"{upg_id}-deploy", "step_id": None, "incident": upg_id, "upgrade": upg_id,
        "task_id": None, "agent": AGENT, "system": system, "op": "deploy_upgrade",
        "params": {"file": patch.rel, "remediation": upg.get("remediation"), "diff_sha256": upg["diff_sha256"]},
        "status": "verified", "system_response": resp,
        "evidence": f"reload ok: version {vb} -> {va}; {patch.rel} sha256 {patcher.sha256(patch.patched)[:12]}",
        "approver": apr.get("decided_by"), "approval_id": apr["_id"], "evidence_incidents": upg.get("evidence"),
        "tests_passed": (upg.get("tests") or {}).get("passed"), "version_before": vb, "version_after": va,
        "ts": ts, "updated_at": ts,
    }
    def record(s):
        col("ledger").update_one({"action_id": receipt["action_id"]}, {"$setOnInsert": receipt},
                                 upsert=True, session=s)
        col("upgrade_requests").update_one(
            {"_id": upg_id, "status": "deploying"},
            {"$set": {"status": "deployed", "version_before": vb, "version_after": va,
                      "deployed_at": ts, "approver": apr.get("decided_by"), "updated_at": ts}}, session=s)

    txn(record)
    set_phase("deployed", upg_id)
    say(f"{upg_id}: deployed - {system} version {vb} -> {va}", system)
    return get(upg_id)


# ------------------------------------------------------------------ 5. prove
def fire_fault(fault: str = PROVE_FAULT, target: str = "mainframe") -> str:
    """Replay the original fault through the Chaos runner (or the systems chaos hook)."""
    try:
        from chaos.runner import fire  # type: ignore
    except ImportError:
        http_post(f"/{target}/_chaos/{fault}", {"params": {}})
        return "http"
    fire(fault, target, {}, manual=False)
    return "chaos.runner"


def _incident_filter(since) -> dict:
    return {"opened_at": {"$gte": since},
            "$or": [{"affected": {"$in": WATCHED_SYSTEMS}}, {"system": {"$in": WATCHED_SYSTEMS}}]}


def prove(upg_id: str, window_s: float = PROVE_WINDOW_S) -> dict:
    """Fire the same fault again; no new incident within the window -> 'proven'."""
    upg = get(upg_id)
    if upg["status"] != "deployed":
        raise StepError(f"{upg_id}: prove needs status 'deployed', found {upg['status']}")
    if _is_tool(upg):
        from . import tool_upgrade  # noqa: PLC0415
        return tool_upgrade.prove(upg)
    system = upg["system"]
    fault = (upg.get("layout") or {}).get("prove_fault") or PROVE_FAULT
    set_phase("proving", upg_id)
    incidents = col("incidents")
    stream = None
    try:  # open the stream BEFORE firing so no incident can slip between fire and watch
        stream = incidents.watch([{"$match": {"operationType": "insert"}}], max_await_time_ms=1000)
    except Exception as e:
        log.warning("incident change stream unavailable (%s); polling", e)
    fired_at = now()
    try:
        via = fire_fault(fault, system)
    except Exception as e:
        if stream:
            stream.close()
        col("upgrade_requests").update_one({"_id": upg_id}, {"$set": {"prove": {
            "fault": fault, "fired_at": fired_at, "window_s": window_s, "incident_opened": None,
            "error": str(e)}}})
        say(f"{upg_id}: could not replay {fault} ({e})", system)
        set_phase(None)
        return get(upg_id)
    col("upgrade_requests").update_one({"_id": upg_id}, {"$set": {"prove": {
        "fault": fault, "fired_at": fired_at, "window_s": window_s, "incident_opened": None, "via": via}}})
    say(f"{upg_id}: replayed {fault} on {system}; watching {int(window_s)}s for incidents", system)

    opened = None
    deadline = time.monotonic() + window_s
    try:
        while time.monotonic() < deadline and opened is None:
            if stream is not None:
                ev = stream.try_next()
                doc = (ev or {}).get("fullDocument") or {}
                if doc and (set(doc.get("affected") or []) | {doc.get("system")}) & set(WATCHED_SYSTEMS):
                    opened = doc["_id"]
            else:
                time.sleep(1.0)
                hit = incidents.find_one(_incident_filter(fired_at), {"_id": 1})
                opened = hit and hit["_id"]
    finally:
        if stream is not None:
            stream.close()
    if opened is None:  # final check also catches incidents that gained `affected` via $addToSet
        hit = incidents.find_one(_incident_filter(fired_at), {"_id": 1})
        opened = hit and hit["_id"]

    prove_doc = {"fault": fault, "fired_at": fired_at, "window_s": window_s,
                 "incident_opened": opened is not None, "incident": opened, "via": via}
    if opened is None:
        advance(upg_id, {"deployed"}, "proven", prove=prove_doc, proven_at=now())
        say(f"{upg_id}: proven - {system} shrugged off {fault}, no incident opened", system)
    else:
        col("upgrade_requests").update_one({"_id": upg_id}, {"$set": {"prove": prove_doc}})
        say(f"{upg_id}: replay still opened {opened}; upgrade stays 'deployed'", system)
    set_phase(None)
    return get(upg_id)


# ------------------------------------------------------------------ whole pipeline + revert
def run(upg_id: str, approval_timeout: float | None = None, prove_window_s: float = PROVE_WINDOW_S) -> dict:
    """Drive an upgrade from its current status to the end. Safe to call again after a crash."""
    try:
        st = get(upg_id)["status"]
        if st in ("requested", "planning"):
            plan(upg_id)
            st = "planning"
        if st in ("planning", "testing"):
            test(upg_id)
            st = "testing"
        if st in ("testing", "awaiting_approval"):
            if gate(upg_id, wait=True, timeout=approval_timeout) != "approved":
                return get(upg_id)
            st = "awaiting_approval"
        if st in ("awaiting_approval", "deploying"):
            deploy(upg_id)
            st = "deployed"
        if st == "deployed" and not get(upg_id).get("prove"):
            prove(upg_id, prove_window_s)
    except Rejected:
        pass
    finally:
        set_phase(None)
    return get(upg_id)


def requeue(upg_id: str) -> dict:
    """Send a rejected upgrade back to 'requested' (e.g. after the system's tests were fixed)."""
    advance(upg_id, {"rejected"}, "requested", reason=None, owner=None)
    say(f"{upg_id} re-queued for another attempt")
    return get(upg_id)


def latest_backup(system: str) -> Path | None:
    files = sorted(BACKUP_DIR.glob(f"{system}.*.py"))
    return files[-1] if files else None


def revert(system: str = "mainframe", reload: bool = True) -> dict:
    """Restore systems/<system>.py from the latest Gardener backup and hot-reload (demo reset)."""
    backup = latest_backup(system)
    if backup is None:
        return {"reverted": False, "reason": f"no backup for {system}"}
    path = inside_systems(SYSTEMS_DIR / f"{system}.py")
    original = backup.read_text()
    changed = not path.exists() or path.read_text() != original
    if changed:
        _write_atomic(path, original)
    resp = None
    if reload:
        try:
            resp = reload_system(system)
        except Exception as e:
            resp = {"error": str(e)}
    col("upgrade_requests").update_many({"system": system, "status": {"$in": ["deployed", "proven"]}},
                                        {"$set": {"reverted_at": now()}})
    say(f"reverted systems/{system}.py from {backup.name}" + ("" if changed else " (already original)"), system)
    return {"reverted": True, "changed": changed, "backup": str(backup), "reload": resp}
