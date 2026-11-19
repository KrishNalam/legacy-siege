"""Legacy Siege war room — ``uvicorn console.server:app --port 8700``.

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
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import autoplay, snapshot
from .api import router
from .stream import Hub

STATIC = Path(__file__).resolve().parent / "static"
log = logging.getLogger("console")


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub = Hub()
    hub.start()
    app.state.hub = hub
    app.state.autoplay = autoplay.start_if_enabled()  # DEMO_AUTOPLAY=1 only
    try:
        yield
    finally:
        if app.state.autoplay:
            app.state.autoplay.stop.set()
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


VIEWS = {"diagram": "diagram.html", "flow": "flow.html"}
PAGES = {**VIEWS, "why": "why.html"}  # /why is a static explainer page, never the / view


def _page(name: str) -> FileResponse:
    return FileResponse(STATIC / PAGES[name], headers={"Cache-Control": "no-cache"})


@app.get("/")
async def index(view: str | None = None) -> FileResponse:
    """The war room page. ``CONSOLE_VIEW`` (diagram | flow, default diagram) picks what ``/`` serves;
    ``?view=diagram|flow`` overrides it."""
    if view in VIEWS:
        return _page(view)
    default = os.environ.get("CONSOLE_VIEW", "diagram").strip().lower()
    return _page(default if default in VIEWS else "diagram")


@app.get("/diagram")
async def diagram_view() -> FileResponse:
    return _page("diagram")


@app.get("/flow")
async def flow_view() -> FileResponse:
    return _page("flow")


@app.get("/why")
async def why_view() -> FileResponse:
    """Why it matters: real-world use cases, why the project is good, Statement 3 point by point. Static, linked from
    the diagram's top bar."""
    return _page("why")


app.mount("/", StaticFiles(directory=STATIC), name="static")
