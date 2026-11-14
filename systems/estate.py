"""The estate engine: ticks, ops, receipts, chaos hooks, reload and reset — all on MongoDB.

MongoDB features used on purpose (legible for the judges):

* **Multi-document transactions** — every atomic change (one system's tick, one op, one
  chaos hook) commits its ``system_state`` kinds, the ``systems`` summary, its log lines and
  the ``op_receipts`` row together, or not at all (``ClientSession.with_transaction``).
* **Exactly-once ops via a unique ``_id``** — the receipt is inserted *first* with
  ``_id = action_id``; a transport retry hits ``DuplicateKeyError`` and gets the stored
  receipt back instead of running the op twice.
* **Atomic claim of delayed ops** — the tick claims due ``slow_apply`` receipts with
  ``find_one_and_update({apply_at <= now, applied_at: null}, {$set: {applied_at}})`` inside
  the same transaction that applies their effect, so a crash never double-applies or loses one.
* **Atomic version bump** — ``find_one_and_update({$inc: {version: 1}})`` on reload.

One process-wide lock serialises writers (HTTP threads + the tick thread) so ops and ticks
never interleave; reads of single documents go straight to MongoDB.
"""
from __future__ import annotations

import importlib
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import CollectionInvalid, DuplicateKeyError

from common.db import txn, world
from systems.base import Ctx, LegacySystem, now_ms

ORDER = ("mainframe", "unix", "portal")  # upstream first: downstream sees this tick's upstream state
MODULES = {sid: f"systems.{sid}" for sid in ORDER}
WORLD_COLLECTIONS = ("systems", "system_state", "system_logs", "op_receipts")


class UnknownSystem(KeyError):
    pass


class Resetting(RuntimeError):
    """The system's documents are being wiped/re-seeded (a war-room reset is in flight): retry shortly."""


class Estate:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.handlers: dict[str, LegacySystem] = {sid: importlib.import_module(m).SYSTEM for sid, m in MODULES.items()}

    # ------------------------------------------------------------ plumbing
    @property
    def db(self):
        return world()

    def handler(self, sid: str) -> LegacySystem:
        try:
            return self.handlers[sid]
        except KeyError:
            raise UnknownSystem(sid) from None

    def _txn(self, fn):
        """Run fn(session) in a MongoDB transaction (retried on transient errors)."""
        return txn(fn)

    def _ctx(self, sid: str, now: datetime, session) -> Ctx:
        return Ctx(self.handler(sid), now, self.db, session, self.handlers)

    def _commit(self, ctx: Ctx, session) -> None:
        """Write one system's changed state kinds, its summary doc and its new logs."""
        db, h = self.db, ctx.system
        for kind, data in ctx.dirty().items():
            db.system_state.update_one(
                {"_id": f"{ctx.id}:{kind}"},
                {"$set": {"system": ctx.id, "kind": kind, "data": data, "updated_at": ctx.now}},
                upsert=True, session=session)
        ctx.faults = [f for f in ctx.faults if not h.fault_resolved(ctx, f["fault"])]
        summary = {**h.summary(ctx), "faults_active": ctx.faults}
        if any(ctx.sysdoc.get(k) != v for k, v in summary.items()):  # no-op ticks stay off the change stream
            db.systems.update_one({"_id": ctx.id}, {"$set": {**summary, "updated_at": ctx.now},
                                                   "$setOnInsert": {"version": 1}}, upsert=True, session=session)
        if ctx.logs:
            db.system_logs.insert_many(ctx.logs, session=session)

    # ------------------------------------------------------------ lifecycle
    def ensure(self) -> None:
        """Create collections/indexes we rely on (idempotent) and seed any missing system."""
        db = self.db
        existing = set(db.list_collection_names())
        for name in WORLD_COLLECTIONS:
            if name not in existing:
                try:
                    db.create_collection(name)
                except CollectionInvalid:
                    pass
        db.system_logs.create_index([("system", ASCENDING), ("ts", ASCENDING)])
        db.system_state.create_index([("system", ASCENDING), ("kind", ASCENDING)])
        db.op_receipts.create_index([("apply_at", ASCENDING)], name="pending_delayed_ops",
                                    partialFilterExpression={"applied_at": None, "apply_at": {"$type": "date"}})
        with self.lock:
            for sid in ORDER:
                if db.systems.find_one({"_id": sid}, {"_id": 1}) is None:
                    self._seed(sid)

    def _seed(self, sid: str, banner: bool = True) -> None:
        now = now_ms()

        def fn(s):
            ctx = self._ctx(sid, now, s)
            ctx.sysdoc, ctx.faults = {}, []
            if banner:
                ctx.log("INFO", "BOOT", BOOT_BANNERS[sid])
            self._commit(ctx, s)
            self.db.systems.update_one({"_id": sid}, {"$set": {"version": 1}}, session=s)
        self._txn(fn)

    def reset(self) -> dict:
        """Baseline: version 1, all up, vulns open/unexploited, no faults, no pending ops, no receipts."""
        with self.lock:
            for name in WORLD_COLLECTIONS:
                self.db[name].delete_many({})
            for sid in ORDER:
                self._seed(sid)
            return {"ok": True, "systems": {sid: self.health(sid) for sid in ORDER}}

    def reload(self, sid: str) -> dict:
        """Re-import systems/<sid>.py, swap the live handler, bump world.systems.version by 1."""
        self.handler(sid)
        with self.lock:
            module = importlib.reload(sys.modules[MODULES[sid]])  # SyntaxError etc. propagate; old handler stays
            self.handlers[sid] = module.SYSTEM
            doc = self.db.systems.find_one_and_update({"_id": sid}, {"$inc": {"version": 1}, "$set": {"updated_at": now_ms()}},
                                                      return_document=ReturnDocument.AFTER)
            after = doc["version"]

            def fn(s):
                ctx = self._ctx(sid, now_ms(), s)
                ctx.log("INFO", "RELOAD", RELOAD_LINES[sid].format(v=after))
                self._commit(ctx, s)
            self._txn(fn)
            return {"system": sid, "version_before": after - 1, "version_after": after}

    # ------------------------------------------------------------ the 1 s tick
    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            with self.lock:
                for sid in ORDER:
                    self._tick_one(sid)

    def _tick_one(self, sid: str) -> None:
        h = self.handler(sid)

        def fn(s):
            now = now_ms()
            ctx = self._ctx(sid, now, s)
            ctx["runtime"]["ticks"] += 1
            # land delayed (slow_apply) ops whose time has come — claimed atomically, applied in this txn
            while True:
                rec = self.db.op_receipts.find_one_and_update(
                    {"system": sid, "applied_at": None, "apply_at": {"$lte": now}},
                    {"$set": {"applied_at": now}}, sort=[("apply_at", ASCENDING)],
                    return_document=ReturnDocument.AFTER, session=s)
                if rec is None:
                    break
                applied, msg = h.apply(ctx, rec["op"], rec.get("params") or {})
                self.db.op_receipts.update_one({"_id": rec["_id"]}, {"$set": {"effective": applied, "late_message": msg}},
                                               session=s)
            h.tick(ctx)
            self._commit(ctx, s)
        self._txn(fn)

    # ------------------------------------------------------------ ops (the honesty rules live here)
    def op(self, sid: str, op: str, action_id: str, params: dict | None) -> dict:
        h = self.handler(sid)
        params = params or {}
        with self.lock:
            stored = self.db.op_receipts.find_one({"_id": action_id})
            if stored:
                return self._receipt_response(stored, duplicate=True)
            now = now_ms()
            receipt = {"_id": action_id, "system": sid, "op": op, "params": params, "accepted": False,
                       "claimed_applied": False, "effective": False, "lied": False, "apply_at": None,
                       "applied_at": None, "message": "", "ts": now}

            def fn(s):
                r = dict(receipt)
                ctx = self._ctx(sid, now, s)
                chaos = ctx["chaos"]
                if h.op_spec(op) is None:
                    r["message"] = f"unknown op {op!r} for {sid}"
                elif chaos["lying"] > 0:  # ACK != APPLIED: claim success, do nothing
                    chaos["lying"] -= 1
                    h.lie_log(ctx, op)
                    r.update(accepted=True, claimed_applied=True, effective=False, lied=True, message="OK - applied")
                elif chaos["slow"]["count"] > 0:  # accepted is not done: effect lands later via the tick
                    chaos["slow"]["count"] -= 1
                    r.update(accepted=True, claimed_applied=False, effective=None,
                             apply_at=now + timedelta(seconds=chaos["slow"]["delay_s"]),
                             message="ACCEPTED - queued for execution")
                else:
                    applied, msg = h.apply(ctx, op, params)
                    r.update(accepted=True, claimed_applied=applied, effective=applied, applied_at=now, message=msg)
                self.db.op_receipts.insert_one(r, session=s)  # unique _id = action_id → exactly once
                self._commit(ctx, s)
                return r

            try:
                r = self._txn(fn)
            except DuplicateKeyError:  # a concurrent retry from another process won the race
                return self._receipt_response(self.db.op_receipts.find_one({"_id": action_id}), duplicate=True)
            return self._receipt_response(r)

    @staticmethod
    def _receipt_response(r: dict, duplicate: bool = False) -> dict:
        # a lie stays a lie on retry; a slow op that has since landed reports applied
        applied = bool(r.get("claimed_applied") or (r.get("applied_at") and r.get("effective")))
        out = {"accepted": r["accepted"], "applied": applied, "message": r.get("message", ""), "action_id": r["_id"]}
        if duplicate:
            out["duplicate"] = True
        return out

    # ------------------------------------------------------------ chaos hooks (C only)
    def chaos(self, sid: str, fault: str, params: dict | None) -> dict:
        h = self.handler(sid)
        params = params or {}
        if fault not in h.faults():
            return {"ok": False, "effect": f"{fault} does not apply to {sid} (valid: {', '.join(h.faults())})"}
        with self.lock:
            def fn(s):
                ctx = self._ctx(sid, now_ms(), s)
                ok, effect = h.chaos(ctx, fault, params)
                if ok:
                    ctx.add_fault(fault, params)
                self._commit(ctx, s)
                return {"ok": ok, "effect": effect}
            return self._txn(fn)

    def clear_chaos(self) -> dict:
        """Disarm ongoing injectors (lying, slow_apply, session leak) and drop not-yet-landed delayed ops.
        Damage already done (ABENDed batch, full disk, stale cache...) stays: healing is the Hive's job;
        POST /_admin/reset restores the full baseline."""
        cleared = {}
        with self.lock:
            for sid in ORDER:
                def fn(s, sid=sid):
                    ctx = self._ctx(sid, now_ms(), s)
                    ctx["chaos"]["lying"] = 0
                    ctx["chaos"]["slow"]["count"] = 0
                    if "sessions" in ctx.state:
                        ctx["sessions"]["leak"] = False
                    dropped = self.db.op_receipts.delete_many({"system": sid, "applied_at": None,
                                                               "apply_at": {"$ne": None}}, session=s).deleted_count
                    self._commit(ctx, s)
                    return dropped
                cleared[sid] = {"pending_ops_dropped": self._txn(fn)}
        return {"ok": True, "cleared": cleared}

    # ------------------------------------------------------------ reads
    def _sysdoc(self, sid: str) -> dict:
        self.handler(sid)
        doc = self.db.systems.find_one({"_id": sid})
        if doc is None:
            with self.lock:
                self._seed(sid)
            doc = self.db.systems.find_one({"_id": sid})
        if doc is None:  # wiped again between our seed and read: a reset is in progress
            raise Resetting(sid)
        return doc

    def health(self, sid: str) -> dict:
        d = self._sysdoc(sid)
        return {"system": sid, "status": d["status"], "blames": d.get("blames"), "version": d["version"]}

    def metrics(self, sid: str) -> dict:
        d = self._sysdoc(sid)
        m = dict(d.get("metrics") or {})
        if m.get("last_batch_ok"):
            m["last_batch_ok"] = m["last_batch_ok"].isoformat()
        keys = ("latency_ms", "error_rate", "queue_depth", "disk_pct", "last_batch_ok", "batch_status", "sessions")
        return {"system": sid, "version": d["version"], **{k: m.get(k) for k in keys}}

    def logs(self, sid: str, since: datetime | None = None, limit: int = 50) -> dict:
        self.handler(sid)
        q: dict = {"system": sid}
        if since:
            q["ts"] = {"$gt": since}
        docs = list(self.db.system_logs.find(q).sort([("ts", DESCENDING), ("_id", DESCENDING)]).limit(max(1, min(limit, 500))))
        docs.reverse()
        return {"system": sid, "lines": [{"ts": d["ts"].isoformat(), "level": d["level"], "code": d["code"],
                                          "line": d["line"]} for d in docs]}

    def ops(self, sid: str) -> list[dict]:
        return [o.catalog() for o in self.handler(sid).OPS]

    def state(self, sid: str) -> dict:
        h = self.handler(sid)
        with self.lock:
            ctx = self._ctx(sid, now_ms(), None)
            return {**h.state_view(ctx), "version": self._sysdoc(sid)["version"]}

    def probe(self, sid: str, vuln_id: str) -> dict:
        h = self.handler(sid)
        with self.lock:
            exploitable, detail = h.probe(self._ctx(sid, now_ms(), None), h.vuln_id(vuln_id))  # KeyError → 404
        return {"vuln_id": vuln_id, "exploitable": exploitable, "detail": detail}

    def view(self, sid: str, name: str, query: dict) -> tuple[str, str]:
        h = self.handler(sid)
        with self.lock:
            return h.view(self._ctx(sid, now_ms(), None), name, query)

    def source(self, sid: str) -> dict:
        self.handler(sid)
        path = Path(sys.modules[MODULES[sid]].__file__)
        return {"system": sid, "module": MODULES[sid], "path": f"systems/{path.name}",
                "version": self._sysdoc(sid)["version"], "source": path.read_text()}


BOOT_BANNERS = {
    "mainframe": "IEA371I SYS1.IPLPARM ON DEVICE 0A82 SELECTED FOR IPL PARAMETERS - MVS1 z/OS 1.13 READY",
    "unix": "kernel: SunOS Release 5.8 Version Generic_117350-62 64-bit; xferhost01 multi-user mode",
    "portal": "INFO  [main] org.apache.catalina.startup.Catalina - Server startup in 48211 ms (NetBank 3.2.1)",
}
RELOAD_LINES = {
    "mainframe": "CSV028I MODULE NIGHTBAT LOADED FROM SYS1.LINKLIB - LLA REFRESH COMPLETE (LEVEL {v})",
    "unix": "pkgadd: SUNWxfer patch level {v} installed; svcadm refresh xfer",
    "portal": "INFO  [main] o.a.c.startup.HostConfig - Redeploying /netbank (build {v})",
}
