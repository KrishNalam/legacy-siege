"""Isolated DBs for common/ tests (llm's guard counter writes to swarm.stats), dropped afterwards."""
from __future__ import annotations

import os

_SFX = os.environ.get("TEST_DB_SUFFIX", "")
DBS = (f"world_test_common{_SFX}", f"swarm_test_common{_SFX}")
os.environ["WORLD_DB"], os.environ["SWARM_DB"] = DBS
os.environ.setdefault("PACE", "0")

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _drop_test_dbs():
    yield
    try:
        from common.db import client  # noqa: PLC0415
        for name in DBS:
            client().drop_database(name)
    except Exception:  # no MongoDB: nothing was written
        pass
