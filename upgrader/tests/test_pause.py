"""UPGRADER_PAUSE: switch off = no pause ever; on = everyone holds during the upgrade and resumes after it,
and an Upgrader that dies mid-upgrade (nobody renews the lease) never leaves agents paused."""
from __future__ import annotations

import threading
import time

import pytest

from common import pause
from common.db import client, col
from upgrader.tests.conftest import SWARM_DB, WORLD_DB


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("UPGRADER_PAUSE", "1")
    monkeypatch.setenv("UPGRADER_PAUSE_LEASE_S", "2")
    col("stats").delete_many({"_id": pause.DOC})
    yield
    col("stats").delete_many({"_id": pause.DOC})


@pytest.fixture(scope="module", autouse=True)
def drop_dbs():
    yield
    for name in (SWARM_DB, WORLD_DB):
        client().drop_database(name)


def test_switch_off_never_pauses(monkeypatch):
    monkeypatch.delenv("UPGRADER_PAUSE")
    with pause.hold("upgrader-1", "mainframe", "UPG-1", "x") as doc:
        assert doc is None and pause.current(fresh=True) is None
    assert col("stats").find_one({"_id": pause.DOC}) is None


def test_hold_pauses_then_resumes_and_waiters_block_meanwhile():
    released = []
    with pause.hold("upgrader-1", "unix", "UPG-2", "Upgrader upgrading unix"):
        assert pause.current(fresh=True)["system"] == "unix"
        t = threading.Thread(target=lambda: released.append(pause.wait_while_paused(lambda: False, poll_s=0.1)))
        t.start()
        time.sleep(2.5)  # longer than the lease: the renewer keeps it alive
        assert not released and pause.current(fresh=True) is not None
    t.join(3)
    assert released == [True] and pause.current(fresh=True) is None


def test_dead_upgrader_lease_lapses_and_agents_resume():
    pause.begin("upgrader-1", "mainframe", "UPG-3", "Upgrader upgrading mainframe")  # then it "dies": no renewal
    t0 = time.monotonic()
    assert pause.wait_while_paused(lambda: False, poll_s=0.1) is True
    assert time.monotonic() - t0 < 3.5  # resumed within the 2 s lease (+ polling)
