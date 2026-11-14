"""FastAPI app for the simulated legacy estate: ``systems.app:app`` on :8701 (CONTRACTS §2).

    .venv/bin/python -m uvicorn systems.app:app --host 0.0.0.0 --port 8701

Routes (``{sys}`` ∈ mainframe | unix | portal):
  GET  /{sys}/health | /metrics | /logs?since=&limit= | /ops | /state | /source
  POST /{sys}/ops/{op}                  {action_id, params} → {accepted, applied, message, action_id}
  POST /{sys}/security/probe/{vuln_id}  → {vuln_id, exploitable, detail}
  GET  /{sys}/view/{name}               legacy screens (mainframe: queue, jcl · unix: xfer.conf, crontab, df ·
                                        portal: balance?acct=, search?q=)
  POST /{sys}/_chaos/{fault}            {params} → {ok, effect}        (Chaos Monkey only)
  POST /_chaos/clear
  POST /_admin/reset · /_admin/reload/{sys} · /_admin/tick?n=1

A 1-second tick loop (``SYSTEMS_TICK_S``, 0 disables it — tests drive /_admin/tick instead)
runs in a worker thread so HTTP stays responsive.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from systems.estate import Estate, Resetting, UnknownSystem

log = logging.getLogger("systems")


class OpRequest(BaseModel):
    action_id: str = Field(min_length=1)
    params: dict = Field(default_factory=dict)


class ChaosRequest(BaseModel):
    params: dict = Field(default_factory=dict)


def _parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    try:
        # an unencoded '+' in "...+00:00" arrives as a space; accept both
        dt = datetime.fromisoformat(re.sub(r" (\d\d:?\d\d)$", r"+\1", since.strip()).replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"bad since: {since!r} (want ISO-8601)") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def create_app(tick_s: float | None = None) -> FastAPI:
    """App factory. ``tick_s`` overrides SYSTEMS_TICK_S (default 1.0; 0 = no background loop)."""
    tick_s = float(os.environ.get("SYSTEMS_TICK_S", 1.0)) if tick_s is None else tick_s
    estate = Estate()

    # SYSTEMS_TICK_FIXED=1 (demo pace, default off): tick on a fixed cadence, i.e. sleep tick_s minus the time
    # the tick itself took. Locally a tick takes milliseconds, so nothing changes; against a remote cluster a
    # tick is several transactions (~2 s at 90 ms RTT) and the default "work, then sleep tick_s" makes every
    # simulated second last ~3 s. A short floor still lets waiting HTTP threads take the estate lock.
    fixed_rate = os.environ.get("SYSTEMS_TICK_FIXED", "").strip().lower() in ("1", "true", "yes", "on")
    floor_s = min(0.2, tick_s)

    async def tick_loop() -> None:
        while True:
            t0 = time.monotonic()
            try:
                await asyncio.to_thread(estate.tick)
            except Exception:  # never let one bad tick kill the estate
                log.exception("tick failed")
            await asyncio.sleep(max(floor_s, tick_s - (time.monotonic() - t0)) if fixed_rate else tick_s)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(estate.ensure)
        task = asyncio.create_task(tick_loop()) if tick_s > 0 else None
        try:
            yield
        finally:
            if task:
                task.cancel()

    app = FastAPI(title="Legacy Siege — legacy estate", lifespan=lifespan)
    app.state.estate = estate

    @app.exception_handler(UnknownSystem)
    async def _unknown(_: Request, exc: UnknownSystem):
        return JSONResponse({"detail": f"unknown system {exc.args[0]!r}"}, status_code=404)

    @app.exception_handler(Resetting)
    async def _resetting(_: Request, exc: Resetting):
        return JSONResponse({"detail": f"{exc.args[0]} is being reset; retry"}, status_code=503,
                            headers={"Retry-After": "1"})

    # ---------------------------------------------------------------- admin + chaos-wide (before /{sys})
    admin = APIRouter(tags=["admin"])

    @admin.post("/_admin/reset")
    def reset():
        return estate.reset()

    @admin.post("/_admin/reload/{sys}")
    def reload(sys: str):
        try:
            return estate.reload(sys)
        except UnknownSystem:
            raise
        except Exception as e:  # e.g. a SyntaxError in a patched module: old code keeps running
            raise HTTPException(500, f"reload of {sys} failed, previous code still live: {type(e).__name__}: {e}") from e

    @admin.post("/_admin/tick")
    def tick(n: int = Query(1, ge=1, le=500)):
        estate.tick(n)
        return {"ok": True, "ticks": n}

    @admin.post("/_chaos/clear")
    def chaos_clear():
        return estate.clear_chaos()

    @admin.get("/")
    def index():
        return {"app": "legacy-siege systems", "systems": {s: estate.health(s) for s in estate.handlers}}

    # ---------------------------------------------------------------- per-system API
    api = APIRouter(prefix="/{sys}", tags=["systems"])

    @api.get("/health")
    def health(sys: str):
        return estate.health(sys)  # always HTTP 200, even when degraded (legacy honesty)

    @api.get("/metrics")
    def metrics(sys: str):
        return estate.metrics(sys)

    @api.get("/logs")
    def logs(sys: str, since: str | None = None, limit: int = Query(50, ge=1, le=500)):
        return estate.logs(sys, _parse_since(since), limit)

    @api.get("/ops")
    def ops(sys: str):
        return estate.ops(sys)

    @api.post("/ops/{op}")
    def run_op(sys: str, op: str, req: OpRequest):
        return estate.op(sys, op, req.action_id, req.params)

    @api.get("/state")
    def state(sys: str):
        return estate.state(sys)

    @api.post("/security/probe/{vuln_id}")
    def probe(sys: str, vuln_id: str):
        try:
            return estate.probe(sys, vuln_id)
        except UnknownSystem:
            raise
        except KeyError:
            raise HTTPException(404, f"unknown vuln {vuln_id!r} on {sys}") from None

    @api.get("/source")
    def source(sys: str, raw: bool = False):
        src = estate.source(sys)
        return PlainTextResponse(src["source"]) if raw else src

    @api.get("/view/{name}")
    def view(sys: str, name: str, request: Request):
        try:
            media, body = estate.view(sys, name, dict(request.query_params))
        except UnknownSystem:
            raise
        except KeyError:
            raise HTTPException(404, f"no view {name!r} on {sys}") from None
        return Response(body, media_type=media)  # 200 even for the stale page, like the real thing

    @api.post("/_chaos/{fault}")
    def chaos(sys: str, fault: str, req: ChaosRequest | None = None):
        result = estate.chaos(sys, fault, req.params if req else {})
        return result if result["ok"] or "does not apply" not in result["effect"] else JSONResponse(result, 400)

    app.include_router(admin)
    app.include_router(api)
    return app


app = create_app()
