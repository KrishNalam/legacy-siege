"""The Gardener process: `python -m gardener.main`. A separate system OUTSIDE the swarm (not a bee).

Registers as swarm.agents/gardener-1 (role 'gardener', target 'workshop'), heartbeats
every HEARTBEAT_S, resumes any upgrade it was in the middle of, then waits for
upgrade_requests with status 'requested' (change stream + polling fallback) and runs
each one through gardener.pipeline. All state is in MongoDB: kill this process at any
point and the next one picks up from the stored status.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from common import config, pause
from common.db import col, now

from . import help as help_desk, pipeline
from .pipeline import AGENT, ROLE

log = logging.getLogger("gardener")
POLL_S = 5.0
IN_FLIGHT = ["planning", "testing", "awaiting_approval", "deploying"]


def register() -> None:
    col("agents").update_one({"_id": AGENT}, {
        "$set": {"role": ROLE, "specialty": "modernization", "status": "idle", "target": "workshop",
                 "incident": None, "task": None, "phase": None, "upgrade": None, "pid": os.getpid(),
                 "last_seen": now(), "started_at": now(), "standby_for": None},
        "$inc": {"generation": 1},
        "$unset": {"died_at": ""},
    }, upsert=True)


def heartbeat(stop: threading.Event) -> None:
    while not stop.wait(config.HEARTBEAT_S):
        try:
            r = col("agents").update_one({"_id": AGENT}, {"$set": {"last_seen": now(), "pid": os.getpid()}})
            if r.matched_count == 0:  # a full demo reset wiped swarm.agents: re-register
                register()
            # if the Undertaker misjudged us (e.g. a long GC pause), come back to life
            col("agents").update_one({"_id": AGENT, "status": "dead"}, {"$set": {"status": "idle"}})
        except Exception as e:  # keep beating through transient DB errors
            log.warning("heartbeat failed: %s", e)


def next_request() -> dict | None:
    return col("upgrade_requests").find_one({"status": "requested"}, sort=[("ts", 1), ("_id", 1)])


def run_safely(upg_id: str) -> None:
    try:
        doc = pipeline.run(upg_id)
        log.info("%s finished with status %s", upg_id, doc.get("status"))
    except pipeline.StepError as e:  # someone else advanced it; not an error
        log.info("%s skipped: %s", upg_id, e)
    except Exception as e:  # never hot-loop on a broken request
        log.exception("%s crashed", upg_id)
        col("upgrade_requests").update_one(
            {"_id": upg_id, "status": {"$nin": ["proven", "rejected", "deployed"]}},
            {"$set": {"status": "rejected", "reason": f"gardener error: {e}", "updated_at": now()}})
        pipeline.set_phase(None)


def open_stream(resume_token=None):
    """Change stream of upgrade requests entering status 'requested'."""
    try:
        return col("upgrade_requests").watch(
            [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]},
                         "fullDocument.status": "requested"}}],
            full_document="updateLookup", max_await_time_ms=1000, resume_after=resume_token)
    except Exception as e:
        log.warning("change stream unavailable (%s); polling every %ss", e, POLL_S)
        return None


def wait_for_request(stream, timeout: float):
    """Return (stream, woke_by_event). Re-opens the stream (resuming) if it errors."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stream is None:
            time.sleep(1.0)
            continue
        try:
            if stream.try_next() is not None:
                return stream, True
        except Exception as e:
            log.warning("change stream error (%s); reopening", e)
            token = stream.resume_token
            stream.close()
            stream = open_stream(token) or open_stream()
    return stream, False


def serve() -> None:
    register()
    if pause.enabled():  # a previous Gardener died mid-upgrade: don't wait for its pause lease to lapse
        pause.end(AGENT)
    stop = threading.Event()
    threading.Thread(target=heartbeat, args=(stop,), daemon=True, name="heartbeat").start()
    if help_desk.enabled():  # GARDENER_HELP=1: answer Sentinel #2 / Worker requests while upgrades run
        threading.Thread(target=help_desk.serve, args=(stop,), daemon=True, name="help").start()
    pipeline.say("Gardener online in the workshop; watching upgrade_requests")
    stream = open_stream()  # opened before the first scan so nothing is missed in between
    try:
        for doc in col("upgrade_requests").find({"status": {"$in": IN_FLIGHT}}).sort("ts", 1):
            pipeline.say(f"resuming {doc['_id']} from status {doc['status']}", doc.get("system"))
            run_safely(doc["_id"])
        while True:
            doc = next_request()
            if doc:
                pipeline.say(f"picked up {doc['_id']}: {doc.get('weakness') or doc.get('fault_class')}",
                             doc.get("system"))
                run_safely(doc["_id"])
                continue
            stream, _ = wait_for_request(stream, POLL_S)
    finally:
        stop.set()
        if stream is not None:
            stream.close()
        col("agents").update_one({"_id": AGENT}, {"$set": {"status": "dead", "died_at": now(), "phase": None}})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s gardener %(levelname)s %(message)s",
                        stream=sys.stdout)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # -> finally: mark ourselves dead
    try:
        serve()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
