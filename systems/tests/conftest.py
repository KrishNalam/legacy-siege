"""Test isolation: every systems test runs against throwaway databases, never 'world'/'swarm'.

An externally provided WORLD_DB/SWARM_DB is honoured only if it is already a test DB
(e.g. the Gardener's WORLD_DB=world_test_gardener); anything else is overridden.
"""
import os

if not os.environ.get("WORLD_DB", "").startswith("world_test"):
    os.environ["WORLD_DB"] = "world_test_systems"
if not os.environ.get("SWARM_DB", "").startswith("swarm_test"):
    os.environ["SWARM_DB"] = "swarm_test_systems"
os.environ["PACE"] = "0"
os.environ["SYSTEMS_TICK_S"] = "0"  # tests drive time with POST /_admin/tick

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from common import config  # noqa: E402
from common.db import client as mongo  # noqa: E402

assert config.WORLD_DB.startswith("world_test") and config.SWARM_DB.startswith("swarm_test")


@pytest.fixture(scope="session")
def app():
    from systems.app import create_app
    yield create_app(tick_s=0)
    for name in (config.WORLD_DB, config.SWARM_DB):
        if name.startswith(("world_test", "swarm_test")):
            mongo().drop_database(name)


@pytest.fixture()
def c(app):
    """A TestClient on a freshly reset estate."""
    with TestClient(app) as tc:
        assert tc.post("/_admin/reset").status_code == 200
        yield tc


@pytest.fixture()
def wdb():
    from common.db import world
    return world()
