"""Isolated DBs (world_test_chaos / swarm_test_chaos), PACE=0, and a real fake systems hook server."""
from __future__ import annotations

import os
import socket
import threading
import time

os.environ["WORLD_DB"] = "world_test_chaos"
os.environ["SWARM_DB"] = "swarm_test_chaos"
os.environ["PACE"] = "0"

import pytest  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from common import config  # noqa: E402
from common.db import client  # noqa: E402

HOOK_PORT = 18731


@pytest.fixture(scope="session", autouse=True)
def isolated_dbs():
    # common.config may already be imported by another suite: force our names on it too
    saved = (config.WORLD_DB, config.SWARM_DB, config.PACE)
    config.WORLD_DB, config.SWARM_DB, config.PACE = "world_test_chaos", "swarm_test_chaos", 0.0
    yield
    client().drop_database("world_test_chaos")
    client().drop_database("swarm_test_chaos")
    config.WORLD_DB, config.SWARM_DB, config.PACE = saved


@pytest.fixture(autouse=True)
def clean_dbs(isolated_dbs):
    for db in ("world_test_chaos", "swarm_test_chaos"):
        for name in client()[db].list_collection_names():
            client()[db][name].delete_many({})
    yield


class FakeSystems:
    """A tiny stand-in for systems.app: records every chaos-hook call."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        app = FastAPI()

        @app.post("/_chaos/clear")
        async def clear():
            self.calls.append(("/_chaos/clear", {}))
            return {"ok": True, "effect": "all faults cleared"}

        @app.post("/{sys}/_chaos/{fault}")
        async def hook(sys: str, fault: str, request: Request):
            body = await request.json()
            self.calls.append((f"/{sys}/_chaos/{fault}", body))
            if sys not in ("mainframe", "unix", "portal"):
                return JSONResponse({"ok": False, "detail": "unknown system"}, status_code=404)
            return {"ok": True, "effect": f"{fault} active on {sys}"}

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=HOOK_PORT, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", HOOK_PORT), timeout=0.1).close()
                return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("fake systems server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture(scope="session")
def fake_server():
    srv = FakeSystems().start()
    yield srv
    srv.stop()


@pytest.fixture
def systems(fake_server, monkeypatch):
    fake_server.calls.clear()
    monkeypatch.setattr(config, "SYSTEMS_URL", f"http://127.0.0.1:{HOOK_PORT}")
    return fake_server
