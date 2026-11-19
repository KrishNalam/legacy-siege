"""Opt-in integration: the hive against workstream A's REAL systems app (systems.app:app), isolated DBs.

    SWARM_REAL_SYSTEMS=1 .venv/bin/python -m pytest agents/tests/test_real_systems.py -q

Skipped by default so the swarm suite stays hermetic while other workstreams change."""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from agents.tests.conftest import LOGS, PY, ROOT, reset_runtime, run_until_resolved, start_swarm, wait_for

pytestmark = pytest.mark.skipif(os.environ.get("SWARM_REAL_SYSTEMS") != "1", reason="set SWARM_REAL_SYSTEMS=1")
PORT = int(os.environ.get("SWARM_REAL_PORT", 18751))


@pytest.fixture(scope="module")
def real(db):
    log = open(LOGS / "test_real_systems.log", "w")
    p = subprocess.Popen([PY, "-m", "uvicorn", "systems.app:app", "--host", "127.0.0.1", "--port", str(PORT),
                          "--log-level", "warning"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    c = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=10)

    def up():
        try:
            return c.get("/mainframe/health").status_code == 200
        except httpx.HTTPError:
            return False
    try:
        wait_for(up, 30, 0.3, "real systems")
        c.post("/_admin/reset")
        reset_runtime()
        s = start_swarm({"SYSTEMS_URL": f"http://127.0.0.1:{PORT}"}, log_name="swarm_real")
        yield c
        s.stop()
    finally:
        p.terminate()
        p.wait(10)
        log.close()


def test_real_round1_and_round2(real):
    from common.db import col
    from agents import recall

    real.post("/mainframe/_chaos/lying_success", json={"params": {"count": 1}})
    real.post("/mainframe/_chaos/batch_abort", json={"params": {}})
    inc = run_until_resolved(1, timeout=180)
    assert inc["diagnosis_path"] == ["portal", "unix", "mainframe"] and inc["fault_class"] == "batch_abort"
    assert len(inc["steps"][0]["attempts"]) == 2  # the lie was caught
    pb = col("playbooks").find_one({"incident": inc["_id"]})
    wait_for(lambda: any(h["_id"] == pb["_id"] for h in recall.search(pb["symptom_text"])[1]), 30, 0.5, "indexed")

    real.post("/_admin/reset")
    wait_for(lambda: all(real.get(f"/{s}/health").json()["status"] == "up" for s in ("mainframe", "unix", "portal")),
             10, 0.3, "estate healthy")
    real.post("/mainframe/_chaos/batch_abort", json={"params": {}})
    inc2 = run_until_resolved(2, timeout=180)
    assert inc2["recall"]["used"] and inc2["metrics"]["hops"] < inc["metrics"]["hops"]
