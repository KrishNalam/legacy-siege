"""The war room's only data source: ONE MongoDB change stream, fanned out to every browser.

    MongoDB (world + swarm)  ──watch()──►  Hub  ──asyncio queues──►  N WebSocket clients

* A single **cluster-level change stream** with a ``$match`` on ``ns`` covers both databases
  (Red's ``world`` and Blue's ``swarm``), opened with ``fullDocument: "updateLookup"`` so every
  update arrives with the whole current document.
* The resume token is persisted to ``swarm.stats {_id: "stream_tokens"}`` (throttled to 1/s),
  so a restarted war room resumes exactly where it stopped instead of missing events.
* Noise is handled server-side: heartbeat-only updates (``last_seen`` / ``lease_until``) are
  counted, not forwarded; bulky collections (``system_state``, ``checkpoint_writes``) are counted
  only, and their documents are removed inside the change-stream pipeline itself.
* Once a second the hub broadcasts a ``tick`` with ``estimatedDocumentCount`` per collection and
  the measured change-events-per-second — the "MongoDB spine" strip at the bottom of the screen.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from datetime import datetime, timezone

from pymongo import AsyncMongoClient, DESCENDING
from pymongo.errors import OperationFailure, PyMongoError

from common import config

from .bson_json import slim, to_json

log = logging.getLogger("warroom.stream")

# logical db -> collections the war room watches
WATCHED: dict[str, list[str]] = {
    "world": ["systems", "chaos_events", "runs", "system_logs", "op_receipts", "system_state"],
    "swarm": ["agents", "thoughts", "incidents", "tasks", "ledger", "approvals", "upgrade_requests",
              "playbooks", "messages", "checkpoints", "checkpoint_writes", "stats", "schema_mappings"],
}
# counted (spine pulses) but never forwarded: high-volume / opaque documents
COUNT_ONLY = {"system_state", "checkpoint_writes"}
# an update touching only these fields is a heartbeat / lease renewal: aggregate, don't animate
QUIET_FIELDS = {"last_seen", "lease_until", "updated_at", "heartbeat_at", "pid"}
TOKEN_DOC = "stream_tokens"


def db_names() -> dict[str, str]:
    """logical -> actual database name (WORLD_DB / SWARM_DB envs, e.g. world_dev)."""
    return {"world": config.WORLD_DB, "swarm": config.SWARM_DB}


def logical_db(actual: str) -> str:
    for logical, name in db_names().items():
        if name == actual:
            return logical
    return actual


def change_pipeline() -> list[dict]:
    """$match on namespace + op type, then drop full documents of count-only collections."""
    names = db_names()
    return [
        {"$match": {
            "operationType": {"$in": ["insert", "update", "replace", "delete"]},
            "$or": [{"ns.db": names[db], "ns.coll": {"$in": colls}} for db, colls in WATCHED.items()],
            "documentKey._id": {"$ne": TOKEN_DOC},  # our own token writes must not echo back
        }},
        {"$set": {"fullDocument": {"$cond": [{"$in": ["$ns.coll", sorted(COUNT_ONLY)]},
                                             "$$REMOVE", "$fullDocument"]}}},
    ]


def _iso(dt: datetime | None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def to_message(change: dict) -> tuple[dict | None, str, bool]:
    """Change event -> (ws message or None, collection, is_quiet)."""
    ns = change.get("ns", {})
    coll = ns.get("coll", "?")
    op = change["operationType"]
    key = to_json(change.get("documentKey", {}).get("_id"))
    quiet = False
    fields: list[str] = []
    if op == "update":
        desc = change.get("updateDescription") or {}
        fields = sorted({f.split(".")[0] for f in (desc.get("updatedFields") or {})}
                        | {f.split(".")[0] for f in (desc.get("removedFields") or [])})
        quiet = bool(fields) and set(fields) <= QUIET_FIELDS
    if coll in COUNT_ONLY or quiet:
        return None, coll, quiet
    msg = {
        "type": "change", "db": logical_db(ns.get("db", "")), "coll": coll, "op": op, "key": key,
        "doc": None if op == "delete" else slim(coll, change.get("fullDocument")),
        "fields": fields, "ts": _iso(change.get("wallTime")),
    }
    return msg, coll, False


class Hub:
    """Owns the change stream and the set of subscriber queues (one per WebSocket)."""

    def __init__(self) -> None:
        self.client: AsyncMongoClient | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.ready = asyncio.Event()          # set once the change stream cursor is open
        self.connected = False
        self.started_at = time.time()
        self.restarts = 0
        self._token: dict | None = None
        self._token_dirty = False
        self._ops = Counter()                  # events per collection since the last tick
        self._heartbeats = 0
        self._tasks: list[asyncio.Task] = []

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.client = AsyncMongoClient(config.MONGODB_URI, tz_aware=True, appname="legacy-siege-warroom",
                                       serverSelectionTimeoutMS=5000)
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._watch_forever(), name="warroom-watch"),
                       loop.create_task(self._tick_forever(), name="warroom-tick"),
                       loop.create_task(self._save_tokens_forever(), name="warroom-tokens")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self._save_token()
        if self.client is not None:
            await self.client.close()

    def world(self):
        return self.client[config.WORLD_DB]

    def swarm(self):
        return self.client[config.SWARM_DB]

    # ---------------------------------------------------------------- fan-out
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def broadcast(self, msg: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:  # a stuck browser tab: tell it to resync instead of blocking everyone
                with contextlib.suppress(Exception):
                    while not q.empty():
                        q.get_nowait()
                    q.put_nowait({"type": "resync", "reason": "client too slow"})

    # ---------------------------------------------------------------- the change stream
    async def _load_token(self) -> dict | None:
        doc = await self.swarm().stats.find_one({"_id": TOKEN_DOC})
        return doc.get("token") if doc else None

    async def _save_token(self) -> None:
        if not self._token_dirty or self._token is None or self.client is None:
            return
        self._token_dirty = False
        with contextlib.suppress(PyMongoError):
            await self.swarm().stats.update_one(
                {"_id": TOKEN_DOC},
                {"$set": {"token": self._token, "saved_at": datetime.now(timezone.utc), "restarts": self.restarts}},
                upsert=True)

    async def _save_tokens_forever(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            await self._save_token()

    async def _watch_forever(self) -> None:
        backoff = 0.5
        first = True
        while True:
            try:
                token = await self._load_token()
                try:
                    stream = await self.client.watch(change_pipeline(), full_document="updateLookup",
                                                     resume_after=token, max_await_time_ms=500)
                except OperationFailure as e:  # token too old / from another deployment: start fresh
                    log.warning("could not resume change stream (%s); starting from now", e.code)
                    stream = await self.client.watch(change_pipeline(), full_document="updateLookup",
                                                     max_await_time_ms=500)
                async with stream:
                    self.connected = True
                    self.ready.set()
                    if not first:
                        self.broadcast({"type": "resync", "reason": "change stream reopened"})
                    first = False
                    backoff = 0.5
                    log.info("change stream open (resumed=%s)", bool(token))
                    while stream.alive:
                        change = await stream.try_next()
                        if stream.resume_token is not None and stream.resume_token != self._token:
                            self._token, self._token_dirty = stream.resume_token, True
                        if change is not None:
                            await self._handle(change)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network blip, server restart: reopen and resume from the token
                self.connected = False
                self.restarts += 1
                log.warning("change stream error: %s; reconnecting in %.1fs", e, backoff)
                self.broadcast({"type": "status", "stream": "reconnecting", "error": str(e)[:200]})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    async def _handle(self, change: dict) -> None:
        msg, coll, quiet = to_message(change)
        self._ops[coll] += 1
        if quiet:
            self._heartbeats += 1
        if msg is not None:
            self.broadcast(msg)
            if coll == "runs" and msg["op"] == "insert":
                await self.mirror_current_run()

    async def mirror_current_run(self) -> dict | None:
        """world.runs -> swarm.stats{_id:"current"} so Blue can tag incidents with a round without
        ever reading Red's database. Reads the newest run, so replays after a resume are harmless."""
        run = await self.world().runs.find_one({}, sort=[("started_at", DESCENDING)])
        if not run:
            return None
        await self.swarm().stats.update_one(
            {"_id": "current"},
            {"$set": {"run_id": run["_id"], "round": run.get("round"), "scenario": run.get("scenario"),
                      "ts": datetime.now(timezone.utc)}},
            upsert=True)
        return run

    # ---------------------------------------------------------------- 1 Hz spine tick
    async def counts(self) -> dict[str, int]:
        async def one(db, coll):
            try:
                return coll, await db[coll].estimated_document_count()
            except PyMongoError:
                return coll, 0

        jobs = [one(self.world(), c) for c in WATCHED["world"]]
        jobs += [one(self.swarm(), c) for c in WATCHED["swarm"] if c != "stats"]
        return dict(await asyncio.gather(*jobs))

    async def _tick_forever(self) -> None:
        last, n, counts = time.monotonic(), 0, {}
        while True:
            await asyncio.sleep(1.0)
            nowm = time.monotonic()
            dt, last = max(nowm - last, 1e-3), nowm
            ops, self._ops = self._ops, Counter()
            hb, self._heartbeats = self._heartbeats, 0
            if not self.subscribers:
                continue
            n += 1
            if n % 3 == 1:  # collection counts every 3 s (18 count commands); ops/s stays at 1 Hz
                try:
                    counts = await self.counts()
                except Exception:
                    counts = {}
            self.broadcast({
                "type": "tick", "counts": counts, "ops": dict(ops), "heartbeats": hb,
                "ops_per_sec": round(sum(ops.values()) / dt, 1), "stream": "live" if self.connected else "down",
                "ts": _iso(None),
            })
