"""Isolated DBs (world_test_warroom / swarm_test_warroom), PACE=0, systems URL unreachable."""
from __future__ import annotations

import os

os.environ["WORLD_DB"] = "world_test_warroom"
os.environ["SWARM_DB"] = "swarm_test_warroom"
os.environ["PACE"] = "0"
os.environ["SYSTEMS_URL"] = "http://127.0.0.1:9"  # discard port: resets must survive a dead systems app

import pytest  # noqa: E402

from common import config  # noqa: E402
from common.db import client  # noqa: E402

DBS = ("world_test_warroom", "swarm_test_warroom")


@pytest.fixture(scope="session", autouse=True)
def isolated_dbs():
    # common.config may already be imported by another suite: force our names on it too
    saved = (config.WORLD_DB, config.SWARM_DB, config.PACE, config.SYSTEMS_URL)
    config.WORLD_DB, config.SWARM_DB, config.PACE, config.SYSTEMS_URL = (*DBS, 0.0, "http://127.0.0.1:9")
    for d in DBS:
        client().drop_database(d)
    yield
    for d in DBS:
        client().drop_database(d)
    config.WORLD_DB, config.SWARM_DB, config.PACE, config.SYSTEMS_URL = saved


@pytest.fixture()
def clean():
    """Empty both test DBs before a test."""
    for d in DBS:
        for c in client()[d].list_collection_names():
            client()[d][c].delete_many({})
    yield
