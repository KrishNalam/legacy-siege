"""Legacy Siege war room — ``uvicorn warroom.server:app --port 8700``.

A pure *watcher* of MongoDB: the page is driven only by change streams (see ``stream.py``).
WebSocket protocol (``/ws``):

1. subscribe to the hub (the shared change stream is already open, so nothing is missed),
2. send ``{"type": "snapshot", ...}`` read from MongoDB,
3. stream ``{"type": "change", db, coll, op, key, doc, fields, ts}`` and a 1 Hz ``{"type": "tick", ...}``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import snapshot
from .api import router
from .stream import Hub

STATIC = Path(__file__).resolve().parent / "static"
log = logging.getLogger("warroom")


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub = Hub()
    hub.start()
    app.state.hub = hub
    try:
        yield
    finally:
        await hub.stop()


app = FastAPI(title="Legacy Siege war room", lifespan=lifespan)
app.include_router(router)


@app.get("/healthz")
async def healthz() -> dict:
    hub: Hub = app.state.hub
    return {"ok": True, "stream": hub.connected, "clients": len(hub.subscribers), "restarts": hub.restarts}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    hub: Hub = websocket.app.state.hub
    queue = hub.subscribe()  # 1) attach to the live change stream FIRST ...
    try:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(hub.ready.wait(), timeout=10)
        snap = await snapshot.build(hub)  # 2) ... THEN read the snapshot; buffered changes replay after it
        await websocket.send_text(json.dumps(snap))
        while True:  # 3) stream (changes are full documents, so re-applying a buffered one is idempotent)
            msg = await queue.get()
            await websocket.send_text(json.dumps(msg))
            if msg.get("type") == "resync":
                await websocket.close(code=4000)
                return
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        log.exception("websocket failed")
    finally:
        hub.unsubscribe(queue)


@app.get("/")
async def index(classic: str | None = None) -> FileResponse:
    """Default page: the simple stage view. ``?classic=1`` (or ``/classic``) serves the full classic war room."""
    page = "hive.html" if classic is None or classic.lower() in ("0", "false", "no") else "index.html"
    return FileResponse(STATIC / page, headers={"Cache-Control": "no-cache"})


@app.get("/classic")
async def classic_view() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=STATIC), name="static")
