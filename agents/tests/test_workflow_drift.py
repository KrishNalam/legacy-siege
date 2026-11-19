"""WORKFLOW_DRIFT=1 contract (Statement 3 Q3, "workflow changes underneath it"), real systems app + real swarm.

Night 1: the mainframe's JES2 maintenance makes restart_batch refuse ("issue release_hold before restart_batch").
The worker notices the refusal, reads /mainframe/ops, the rules insert release_hold, the Guard verifies it and
the retried restart_batch, and the workflow is stored in swarm.schema_mappings (kind workflow, v2).
Night 2: the scout plans release_hold up front from memory: no refusal, one attempt. No Claude call either night."""
from __future__ import annotations

import socket
import subprocess

import httpx
import pytest

from agents.tests.conftest import LOGS, PY, ROOT, reset_runtime, run_until_resolved, start_swarm, wait_for


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def real(db):
    port = _free_port()
    log = open(LOGS / "test_workflow_systems.log", "w")
    p = subprocess.Popen([PY, "-m", "uvicorn", "systems.app:app", "--host", "127.0.0.1", "--port", str(port),
                          "--log-level", "warning"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)

    def up():
        try:
            return c.get("/mainframe/health").status_code == 200
        except httpx.HTTPError:
            return False
    try:
        wait_for(up, 30, 0.3, "real systems")
        c.post("/_admin/reset")
        reset_runtime()
        db.schema_mappings.delete_many({})
        s = start_swarm({"SYSTEMS_URL": f"http://127.0.0.1:{port}", "WORKFLOW_DRIFT": "1"}, log_name="swarm_workflow")
        yield c
        s.stop()
    finally:
        p.terminate()
        p.wait(10)
        log.close()


def _ledger_ops(inc_id: str) -> list[tuple[str, str]]:
    from common.db import col
    return [(r["op"], r["status"]) for r in col("ledger").find({"incident": inc_id}).sort("updated_at", 1)]


def test_workflow_drift_learned_then_reused(real):
    from common.db import col

    real.post("/mainframe/_chaos/workflow_drift", json={"params": {}})
    real.post("/mainframe/_chaos/batch_abort", json={"params": {}})
    inc = run_until_resolved(1, timeout=180)
    wd = inc["workflow_drift"]
    assert wd["requires"] == ["release_hold"] and wd["op"] == "restart_batch" and wd["proposed_by"] == "rules"
    assert "issue release_hold before restart_batch" in wd["message"]
    by_op = {s["op"]: s for s in inc["steps"]}
    assert by_op["release_hold"]["status"] == by_op["restart_batch"]["status"] == "verified"
    assert len(by_op["restart_batch"]["attempts"]) == 2          # refused once, then verified after release_hold
    verified = [op for op, st in _ledger_ops(inc["_id"]) if st == "verified"]
    assert verified.index("release_hold") < verified.index("restart_batch")
    m = col("schema_mappings").find_one({"_id": "mainframe.workflow.restart_batch"})
    assert m["kind"] == "workflow" and m["requires"] == ["release_hold"] and m["version"] == 2
    assert m["learned_from"] == inc["_id"] and m["verified_by"] == "guard-1"
    assert col("thoughts").find_one({"text": {"$regex": "^workflow changed: restart_batch now needs release_hold"}})
    assert not any(s.get("source") == "ai" for s in inc["steps"])   # no key: the rules path did it

    # night 2: same drift, the plan already has release_hold (memory), nothing is refused
    real.post("/_admin/reset")
    wait_for(lambda: all(real.get(f"/{s}/health").json()["status"] == "up" for s in ("mainframe", "unix", "portal")),
             10, 0.3, "estate healthy")
    real.post("/mainframe/_chaos/workflow_drift", json={"params": {}})
    real.post("/mainframe/_chaos/batch_abort", json={"params": {}})
    inc2 = run_until_resolved(2, timeout=180)
    ops2 = [s["op"] for s in sorted(inc2["steps"], key=lambda s: s["n"])]
    assert ops2.index("release_hold") < ops2.index("restart_batch")
    rh = next(s for s in inc2["steps"] if s["op"] == "release_hold")
    assert rh["workflow"]["memory"] == "mainframe.workflow.restart_batch" and rh["workflow"]["version"] == 2
    assert len(next(s for s in inc2["steps"] if s["op"] == "restart_batch")["attempts"]) == 1   # no refusal
    assert "workflow_drift" not in inc2
    assert col("schema_mappings").find_one({"_id": "mainframe.workflow.restart_batch"})["uses"] == 1
