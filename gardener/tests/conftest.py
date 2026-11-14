"""Isolated DBs for Gardener tests: set BEFORE anything imports `common`."""
import os

os.environ["WORLD_DB"] = "world_test_gardener"
os.environ["SWARM_DB"] = "swarm_test_gardener"
os.environ["PACE"] = "0"
os.environ["GARDENER_REPLAY_WORLD_DB"] = "world_test_gardener_replay"
os.environ["GARDENER_REPLAY_SWARM_DB"] = "swarm_test_gardener_replay"
os.environ["GARDENER_REPLAY_WAIT_S"] = "1"
os.environ.pop("ANTHROPIC_API_KEY", None)

collect_ignore = ["fixtures"]  # the fixture systems package has its own test file
