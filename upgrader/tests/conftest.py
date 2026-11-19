"""Isolated DBs for Gardener tests: set BEFORE anything imports `common`.

TEST_DB_SUFFIX (e.g. "_w2") is appended to every DB name so parallel runs on one MongoDB never collide."""
import os

_SFX = os.environ.get("TEST_DB_SUFFIX", "")
WORLD_DB, SWARM_DB = f"world_test_gardener{_SFX}", f"swarm_test_gardener{_SFX}"
os.environ["WORLD_DB"] = WORLD_DB
os.environ["SWARM_DB"] = SWARM_DB
os.environ["PACE"] = "0"
os.environ["GARDENER_REPLAY_WORLD_DB"] = f"world_test_gardener_replay{_SFX}"
os.environ["GARDENER_REPLAY_SWARM_DB"] = f"swarm_test_gardener_replay{_SFX}"
os.environ["GARDENER_REPLAY_WAIT_S"] = "1"
os.environ.pop("ANTHROPIC_API_KEY", None)

collect_ignore = ["fixtures"]  # the fixture systems package has its own test file
