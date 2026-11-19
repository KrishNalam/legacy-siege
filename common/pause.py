"""Upgrader pause (UPGRADER_PAUSE=1, default OFF): every agent, swarm and Red fleet, holds still while the
Upgrader writes and hot-reloads a system, then carries on.

One doc, `swarm.stats{_id:"upgrader_pause"}` = {active, by, system, upgrade, reason, since, until, ended_at}.
It is a **lease**: the pause counts only while `until` is in the future, and the Upgrader renews it every
few seconds from a background thread for as long as the upgrade runs. If the Upgrader dies mid-upgrade
(SIGKILL, crash, box reboot) nobody renews it, and within UPGRADER_PAUSE_LEASE_S every agent resumes on
its own. A restarted Upgrader also clears it at startup, and a full reset wipes swarm.stats.

The pause is cooperative, not SIGSTOP: agents stop at their next safe point (top of a tick / loop),
keep heartbeating and renewing their work leases, and simply don't start new work. So nobody is declared
dead, no lease expires and no recovery takes over because of the pause.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

from common.db import col, now

DOC = "upgrader_pause"
_CHECK_EVERY_S = 1.0  # an agent re-reads the pause doc at most this often (keeps Atlas round trips low)
_cache: dict = {"at": -1e9, "doc": None}


def enabled() -> bool:
    return os.environ.get("UPGRADER_PAUSE", "").strip().lower() in ("1", "true", "yes", "on")


def lease_s() -> float:
    try:
        return max(2.0, float(os.environ.get("UPGRADER_PAUSE_LEASE_S") or 15))
    except ValueError:
        return 15.0


def current(fresh: bool = False) -> dict | None:
    """The live pause doc, or None (switch off, no pause, ended, or lease run out)."""
    if not enabled():
        return None
    t = time.monotonic()
    if fresh or t - _cache["at"] >= _CHECK_EVERY_S:
        try:
            _cache["doc"] = col("stats").find_one({"_id": DOC, "active": True, "until": {"$gt": now()}})
        except Exception:  # a DB blip never freezes anyone
            _cache["doc"] = None
        _cache["at"] = t
    return _cache["doc"]


def begin(by: str, system: str, upgrade: str | None, reason: str) -> dict:
    ts = now()
    doc = {"active": True, "by": by, "system": system, "upgrade": upgrade, "reason": reason, "since": ts,
           "until": ts + timedelta(seconds=lease_s()), "ended_at": None}
    col("stats").update_one({"_id": DOC}, {"$set": doc}, upsert=True)
    _cache["at"] = -1e9  # this process sees its own change at once
    return doc


def renew(by: str) -> bool:
    r = col("stats").update_one({"_id": DOC, "active": True, "by": by},
                                {"$set": {"until": now() + timedelta(seconds=lease_s())}})
    return r.matched_count == 1


def end(by: str | None = None) -> None:
    q = {"_id": DOC, "active": True}
    if by:
        q["by"] = by
    col("stats").update_one(q, {"$set": {"active": False, "ended_at": now()}})
    _cache["at"] = -1e9


@contextmanager
def hold(by: str, system: str, upgrade: str | None, reason: str, min_s: float = 0.0):
    """Pause everyone for the duration of the block (no-op when the switch is off).

    Renews the lease every lease/3 s from a daemon thread; always ends the pause on the way out.
    `min_s` keeps it up long enough for people to see it on the war room."""
    if not enabled():
        yield None
        return
    doc = begin(by, system, upgrade, reason)
    stop = threading.Event()

    def renewer():
        while not stop.wait(lease_s() / 3):
            try:
                renew(by)
            except Exception:
                pass

    threading.Thread(target=renewer, daemon=True, name="pause-lease").start()
    t0 = time.monotonic()
    try:
        yield doc
        left = min_s - (time.monotonic() - t0)
        if left > 0:
            time.sleep(left)
    finally:
        stop.set()
        try:
            end(by)
        except Exception:
            pass  # the lease runs out on its own


def wait_while_paused(stopped, on_pause=None, on_resume=None, poll_s: float = 0.5) -> bool:
    """Block while a pause is live. `stopped()` -> True aborts the wait. Returns True if it paused."""
    doc = current()
    if doc is None:
        return False
    if on_pause:
        on_pause(doc)
    while not stopped() and current(fresh=True) is not None:
        time.sleep(poll_s)
    if on_resume:
        on_resume()
    return True
