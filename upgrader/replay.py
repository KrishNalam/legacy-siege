"""Regression replay of the original fault against a *patched* copy of the systems package.

Runs in its own subprocess (spawned by upgrader.testing) with:
    cwd        = a temp sandbox that contains the patched `systems/` package
    PYTHONPATH = repo root (for `common`, `upgrader`)
    WORLD_DB   = world_test_upgrader   (never the live `world` DB)
so `import systems.mainframe` resolves to the patched sandbox copy.

Two layers, both must pass:
  1. unit replay   - patched validate_record() accepts good records, rejects the S0C7 poison
                     and length errors (samples built from the module's own layout).
  2. app replay    - only if systems/app.py exists: boot the real FastAPI app in-process
                     (TestClient, isolated DB), reset, fire batch_abort, force a NIGHTBAT run,
                     and assert the batch ends OK with no ABEND (the poison record was rejected).

Prints a human transcript and a final line `REPLAY_RESULT {json}`; exit code 0 = pass.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

WAIT_S = float(os.environ.get("UPGRADER_REPLAY_WAIT_S", 6))


def log(msg: str) -> None:
    print(msg, flush=True)


def unit_replay(system: str) -> bool:
    import importlib

    from upgrader.remediations import batch_validation as bv

    mod = importlib.import_module(f"systems.{system}")
    src_path = Path(mod.__file__).resolve()
    if Path.cwd().resolve() not in src_path.parents:
        log(f"FAIL unit: systems.{system} resolved to {src_path}, not the sandbox")
        return False
    layout = bv.detect_layout(ast.parse(src_path.read_text()))
    log(f"unit: layout from {layout.source}: len={layout.length_expr} "
        f"account={layout.field_expr['account']} amount={layout.field_expr['amount']}")
    samples = bv.sample_records(layout)
    ok = True
    for rec in samples["good"]:
        res = mod.validate_record(rec)
        ok &= res is True
        log(f"  {'ok ' if res is True else 'BAD'} accept {rec!r}")
    for rec in samples["bad"]:
        res = mod.validate_record(rec)
        ok &= res is False
        log(f"  {'ok ' if res is False else 'BAD'} reject {rec!r}")
    # use the system's own ground truth when it exposes it (systems/mainframe.py does)
    if isinstance(getattr(mod, "BAD_SAMPLE_RECORD", None), str):
        res = mod.validate_record(mod.BAD_SAMPLE_RECORD)
        ok &= res is False
        log(f"  {'ok ' if res is False else 'BAD'} reject BAD_SAMPLE_RECORD {mod.BAD_SAMPLE_RECORD!r}")
    if callable(getattr(mod, "is_malformed", None)):
        leaks = [r for r in samples["good"] + samples["bad"] if mod.is_malformed(r) and mod.validate_record(r)]
        ok &= not leaks
        log(f"  {'ok ' if not leaks else 'BAD'} no record the batch would ABEND on passes validation"
            + (f" (leaks: {leaks!r})" if leaks else ""))
    if callable(getattr(mod, "validation_enabled", None)):
        res = mod.validation_enabled()
        ok &= res is True
        log(f"  {'ok ' if res else 'BAD'} validation_enabled() = {res}")
    log(f"unit replay: {'PASS' if ok else 'FAIL'}")
    return ok


def _batch(state: dict) -> dict:
    return state.get("batch") or {}


def app_replay() -> bool:
    """Replay batch_abort through the real systems API (contract §2) on an isolated DB."""
    from fastapi.testclient import TestClient

    from systems.app import app  # the sandbox copy, with the patched mainframe module

    with TestClient(app) as c:
        r = c.post("/_admin/reset")
        log(f"app: reset -> {r.status_code}")
        t0 = datetime.now(timezone.utc).isoformat()
        r = c.post("/mainframe/_chaos/batch_abort", json={"params": {}})
        log(f"app: fire batch_abort -> {r.status_code} {r.text[:200]}")
        if r.status_code >= 400:
            return False
        status = None
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:  # let the tick loop run the batch on its own
            status = _batch(c.get("/mainframe/state").json()).get("status")
            if status in ("OK", "ABENDED"):
                break
            time.sleep(0.5)
        log(f"app: batch status after fault = {status}")
        # force a NIGHTBAT run: with the legacy code this fails while poison remains
        r = c.post("/mainframe/ops/restart_batch",
                   json={"action_id": f"UPGRADER-REPLAY-{int(time.time())}", "params": {"job": "NIGHTBAT"}})
        log(f"app: restart_batch -> {r.status_code} {r.text[:200]}")
        state = {}
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            state = c.get("/mainframe/state").json()
            if _batch(state).get("status") in ("OK", "ABENDED"):
                break
            time.sleep(0.5)
        lines = c.get("/mainframe/logs", params={"since": t0, "limit": 50}).json().get("lines", [])
        abends = [ln for ln in lines if "S0C7" in str(ln.get("line", "")) or "ABEND=" in str(ln.get("line", ""))]
        rejects = [ln for ln in lines if "REJECT" in str(ln.get("line", "")).upper()
                   or "REJECT" in str(ln.get("code", "")).upper()]
        batch = _batch(state)
        log(f"app: final batch = {json.dumps(batch, default=str)[:300]}")
        log(f"app: validation_enabled = {state.get('validation_enabled')}")
        for ln in (abends + rejects)[:6]:
            log(f"  log: {ln.get('code', '')} {ln.get('line', '')}")
        ok = batch.get("status") == "OK" and not abends and state.get("validation_enabled") is not False
        log(f"app replay: batch {'OK' if ok else 'NOT OK'}; abend lines={len(abends)}; "
            f"rejected-record evidence={'yes' if rejects or not batch.get('poison_records') else 'no'}")
        return ok


def _faults(system: str) -> list[str]:
    from common.db import col

    return [f.get("fault") for f in (col("systems").find_one({"_id": system}) or {}).get("faults_active") or []]


def tool_replay(fault: str, system: str, ops: list[dict]) -> bool:
    """Tool upgrade test: fire `fault` on an isolated estate, run the tool's ops, read back.

    Passes only if the fault was really active before the ops and is resolved after them, the system reads
    back healthy and every op was accepted. `$record_id` placeholders bind to the poison records the
    mainframe reports at that moment (as an Investigator binds them from today's diagnosis)."""
    from fastapi.testclient import TestClient

    from systems.app import app

    with TestClient(app) as c:
        log(f"tool: reset -> {c.post('/_admin/reset').status_code}")
        r = c.post(f"/{system}/_chaos/{fault}", json={"params": {}})
        log(f"tool: fire {fault} on {system} -> {r.status_code} {r.text[:160]}")
        if r.status_code >= 400:
            return False
        c.post("/_admin/tick", params={"n": 2})
        before = _faults(system)
        log(f"tool: active faults before = {before}; health = {c.get(f'/{system}/health').json().get('status')}")
        if fault not in before:
            log("tool replay: FAIL, the fault never became active, nothing to prove")
            return False
        poison = (c.get("/mainframe/state").json().get("batch") or {}).get("poison_records") or []
        ok = True
        for i, op in enumerate(ops, 1):
            targets = [dict(op["params"], record_id=rec) for rec in poison] \
                if op["params"].get("record_id") == "$record_id" else [op["params"]]
            for params in targets:
                r = c.post(f"/{op['system']}/ops/{op['op']}",
                           json={"action_id": f"UPGRADER-TOOL-{int(time.time())}-{i}-{len(params)}", "params": params})
                body = r.json() if r.content else {}
                good = r.status_code < 400 and body.get("accepted") is True
                ok &= good
                log(f"  {'ok ' if good else 'BAD'} {op['system']}.{op['op']} {params} -> {r.status_code} "
                    f"{str(body.get('message') or body)[:120]}")
            c.post("/_admin/tick", params={"n": 1})
        c.post("/_admin/tick", params={"n": 3})
        after, health = _faults(system), c.get(f"/{system}/health").json().get("status")
        log(f"tool: active faults after = {after}; health = {health}")
        ok &= fault not in after and health == "up"
        log(f"tool replay: {'PASS' if ok else 'FAIL'}")
        return ok


def main_tool(argv: list[str]) -> int:
    """`python -m upgrader.replay --tool <fault> <system> <ops.json>`"""
    fault, system, ops_path = argv[0], argv[1], argv[2]
    sys.path.insert(0, os.getcwd())
    result = {"tool": None}
    try:
        result["tool"] = tool_replay(fault, system, json.loads(Path(ops_path).read_text()))
    except Exception:
        log(traceback.format_exc(limit=6))
        result["error"] = True
    result["passed"] = bool(result["tool"]) and "error" not in result
    log("REPLAY_RESULT " + json.dumps(result))
    return 0 if result["passed"] else 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--tool":
        return main_tool(sys.argv[2:])
    system = sys.argv[1] if len(sys.argv) > 1 else "mainframe"
    sys.path.insert(0, os.getcwd())  # the sandbox's `systems` must win over the repo copy
    result = {"unit": None, "app": None}
    try:
        result["unit"] = unit_replay(system)
        if (Path.cwd() / "systems" / "app.py").exists():
            result["app"] = app_replay()
        else:
            log("app replay: skipped (systems/app.py not present in sandbox)")
    except Exception:
        log(traceback.format_exc(limit=6))
        result.setdefault("error", True)
    passed = bool(result["unit"]) and result["app"] is not False and "error" not in result
    result["passed"] = passed
    log("REPLAY_RESULT " + json.dumps(result))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
