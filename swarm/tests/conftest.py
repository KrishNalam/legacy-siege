"""Swarm test harness: isolated databases, PACE=0, short leases, a fake systems server on a test port,
and a real swarm supervisor (real OS processes) when a test asks for one."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

FAKE_PORT = int(os.environ.get("SWARM_TEST_PORT", 18731))
_SFX = os.environ.get("SWARM_TEST_DB_SUFFIX", "")  # e.g. "_w2" when several checkouts test on one MongoDB at once
TEST_ENV = {
    "WORLD_DB": f"world_test_swarm{_SFX}", "SWARM_DB": f"swarm_test_swarm{_SFX}",
    "PACE": os.environ.get("SWARM_TEST_PACE", "0"),  # SWARM_TEST_PACE=1 for a demo-paced dry run
    "HEARTBEAT_S": "0.5", "LEASE_S": "3", "DEAD_AFTER_S": "2.5", "NURSE_RESPAWN_S": "2",
    "SENTINEL_POLL_S": "0.2", "SENTINEL_CONFIRM_POLLS": "2", "SWARM_POLL_S": "0.1", "NURSE_POLL_S": "0.2",
    "NURSE_PICKUP_S": "1.5", "SCOUT_SETTLE_S": "0.8", "GUARD_GRACE_S": "0.6", "SLOW_APPLY_S": "3",
    "FAKE_TICK_S": "0.2", "STEP_HOLD_S": "0", "SYSTEMS_URL": f"http://127.0.0.1:{FAKE_PORT}",
    "ANTHROPIC_API_KEY": "", "VOYAGE_API_KEY": "",
}
os.environ.update(TEST_ENV)  # must happen before anything imports `common`

import httpx  # noqa: E402
import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "logs"  # git-ignored; agent/supervisor output of test runs lands here
LOGS.mkdir(exist_ok=True)
PY = sys.executable
RUNTIME = ["incidents", "tasks", "agents", "thoughts", "messages", "ledger", "playbooks", "approvals",
           "upgrade_requests", "stats", "counters", "checkpoints", "checkpoint_writes", "spawn_requests"]


def wait_for(pred, timeout: float = 30, interval: float = 0.1, what: str = "condition"):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture(scope="session")
def db():
    from common import config
    from common.db import client, swarm
    from scripts.setup_db import ensure

    assert config.SWARM_DB.startswith("swarm_test_") and config.WORLD_DB.startswith("world_test_")
    ensure()
    yield swarm()
    client().drop_database(config.SWARM_DB)
    client().drop_database(config.WORLD_DB)


def reset_runtime():
    from common.db import swarm

    for n in RUNTIME:
        swarm()[n].delete_many({})


@pytest.fixture(scope="session")
def fake():
    log = open(LOGS / "test_fake_systems.log", "w")
    p = subprocess.Popen([PY, "-m", "uvicorn", "swarm.tests.fake_systems:app", "--host", "127.0.0.1",
                          "--port", str(FAKE_PORT), "--log-level", "warning"], cwd=ROOT, stdout=log,
                         stderr=subprocess.STDOUT)
    c = httpx.Client(base_url=f"http://127.0.0.1:{FAKE_PORT}", timeout=5)

    def up():
        try:
            return c.get("/mainframe/health").status_code == 200
        except httpx.HTTPError:
            return False
    wait_for(up, 20, 0.2, "fake systems to start")
    yield c
    p.terminate()
    p.wait(10)
    log.close()


class Swarm:
    """A real `python -m swarm.supervisor` process tree."""

    def __init__(self, env_extra: dict | None = None, log_name: str = "swarm"):
        self.log = open(LOGS / f"test_{log_name}.log", "w")
        self.p = subprocess.Popen([PY, "-m", "swarm.supervisor"], cwd=ROOT, env={**os.environ, **(env_extra or {})},
                                  stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)

    def stop(self):
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
            try:
                self.p.wait(15)
            except subprocess.TimeoutExpired:
                os.killpg(self.p.pid, signal.SIGKILL)
        try:  # belt and braces: nothing from this tree may outlive the test
            os.killpg(self.p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.log.close()


def start_swarm(env_extra=None, log_name="swarm", agents: int = 12) -> Swarm:
    from common.db import col

    s = Swarm(env_extra, log_name)
    try:
        wait_for(lambda: col("agents").count_documents({"status": {"$in": ["idle", "standby"]}}) >= agents, 60, 0.2,
                 f"{agents} agents to register")
        wait_for(lambda: col("stats").find_one({"_id": "queen_lease", "owner": "queen-1"}), 30, 0.2,
                 "queen-1 to take the queen lease")
    except BaseException:
        s.stop()  # never leak a process tree when setup fails
        raise
    return s


def approve_pending(decided_by: str = "test-beekeeper") -> list[str]:
    """Play the Beekeeper: approve every pending approval (only pending docs are ever decided)."""
    from common.db import col, now

    ids = []
    # the Beekeeper answers once the queen has relayed the request (keeps the relay path observable)
    for a in col("approvals").find({"status": "pending", "relayed_by": {"$ne": None}}):
        r = col("approvals").update_one({"_id": a["_id"], "status": "pending"},
                                        {"$set": {"status": "approved", "decided_by": decided_by, "decided_at": now()}})
        if r.modified_count:
            ids.append(a["_id"])
    return ids


def run_until_resolved(n_resolved: int, timeout: float = 120, on_tick=None) -> dict:
    """Approve as needed until the n-th incident resolves (or one fails)."""
    from common.db import col

    known_failed = [i["_id"] for i in col("incidents").find({"status": "failed"}, {"_id": 1})]

    def done():
        approve_pending()
        if on_tick:
            on_tick()
        failed = col("incidents").find_one({"status": "failed", "_id": {"$nin": known_failed}})
        assert failed is None, f"incident failed: {failed.get('failure')}"
        return col("incidents").count_documents({"status": "resolved"}) >= n_resolved
    wait_for(done, timeout, 0.2, f"{n_resolved} resolved incident(s)")
    return col("incidents").find_one({"status": "resolved"}, sort=[("closed_at", -1)])
