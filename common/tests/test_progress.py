"""PROGRESS_ETA on/off contract: off writes nothing; on keeps a time-series history, estimates
median/p90 from it (system first, then action type) and emits area-keyed progress events."""
import os

os.environ.setdefault("WORLD_DB", "world_test_progress" + os.environ.get("TEST_DB_SUFFIX", ""))
os.environ.setdefault("SWARM_DB", "swarm_test_progress" + os.environ.get("TEST_DB_SUFFIX", ""))

import pytest  # noqa: E402

from common import progress  # noqa: E402
from common.db import client, swarm, world  # noqa: E402


@pytest.fixture(autouse=True)
def fresh():
    for db in (world(), swarm()):
        client().drop_database(db.name)
    progress._made.clear()
    yield
    for db in (world(), swarm()):
        client().drop_database(db.name)


def test_off_writes_nothing(monkeypatch):
    monkeypatch.delenv("PROGRESS_ETA", raising=False)
    progress.record("swarm", "step:restart_batch", "mainframe", "worker", 2.0, "verified")
    with progress.track("world", "attack:batch_abort", "mainframe", "chaos", agent="x", label="x"):
        pass
    assert progress.estimate("swarm", "step:restart_batch")["k"] == 0
    assert swarm().list_collection_names() == [] and world().list_collection_names() == []


def test_on_history_estimate_and_events(monkeypatch):
    monkeypatch.setenv("PROGRESS_ETA", "1")
    assert progress.eta_text(progress.estimate("swarm", "step:restart_batch", "mainframe")).startswith("ETA unknown")
    for d in (1.0, 2.0, 3.0, 4.0, 20.0):
        progress.record("swarm", "step:restart_batch", "mainframe", "worker", d, "verified")
    info = swarm().command("listCollections", filter={"name": progress.COLL})["cursor"]["firstBatch"][0]
    assert info["type"] == "timeseries" and info["options"]["timeseries"]["metaField"] == "meta"
    est = progress.estimate("swarm", "step:restart_batch", "mainframe")
    assert (est["k"], est["median_s"], est["p90_s"]) == (5, 3.0, 20.0)
    assert progress.estimate("swarm", "step:restart_batch", "unix")["scope"] == "step:restart_batch"  # fallback
    assert progress.eta_text(est) == "ETA ~3 s, p90 20 s (based on 5 past runs)"

    with progress.track("world", "attack:disk_fill", "unix", "red", agent="red-saboteur", label="Red fleet: disk_fill",
                        step=1, of=2):
        pass
    assert world()[progress.COLL].count_documents({"meta.action_type": "attack:disk_fill"}) == 1
    start, done = [t["progress"] for t in swarm().thoughts.find({"kind": "progress"}).sort("ts", 1)]
    assert (start["area"], start["phase"], start["pct"], done["phase"], done["pct"]) == ("chaos", "start", 0, "done", 50)
    assert swarm().stats.find_one({"_id": "progress:chaos"})["phase"] == "done"
