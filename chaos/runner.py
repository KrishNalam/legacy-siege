"""Run scenarios and fire faults. Every fault — scripted or a judge's button — becomes a
``world.chaos_events`` document. The swarm never reads it; the war room and the
scorecard do, to compare "what happened" with "what the Hive figured out".

Importable and thread-safe: the war-room server calls ``run_scenario(name)`` (returns
the run id at once, steps play in a daemon thread) and ``fire(...)`` for manual buttons.

    run_id = start_run("bank-night-1")            # world.runs RUN-0001, round auto-increments
    run_id = run_scenario("bank-night-1")         # start + play steps in the background
    ev     = fire("batch_abort", "mainframe")     # one fault, logged to chaos_events
"""
from __future__ import annotations

import logging
import os
import threading
import time

from pymongo import DESCENDING, ReturnDocument

from common import config, progress
from common.db import next_id, now, swarm, world

from . import decider, faults, scenarios

log = logging.getLogger("chaos.runner")

_start_lock = threading.Lock()                                   # serializes round numbering
_active: dict[str, tuple[threading.Thread, threading.Event]] = {}  # run_id -> (player, cancel flag)
_active_lock = threading.Lock()


def _runs():
    return world()["runs"]


def _events():
    return world()["chaos_events"]


def _next_id(prefix: str) -> str:
    """Atomic counter in the *world* DB (Red never writes Blue's swarm.counters)."""
    return next_id(prefix, db=world())


# ------------------------------------------------------------------ runs
def start_run(scenario: str, round: int | None = None) -> str:
    """Open a new run (closing any still-open one) and return its id, e.g. ``RUN-0003``.

    ``round=None`` -> highest round in world.runs + 1 (a full reset starts again at 1).
    """
    scn = scenarios.get(scenario)
    with _start_lock:
        if round is None:
            last = _runs().find_one({"round": {"$type": "number"}}, sort=[("round", DESCENDING)])
            round = (last["round"] if last else 0) + 1
        ts = now()
        _runs().update_many({"ended_at": None}, {"$set": {"ended_at": ts}})  # one live run at a time
        run_id = _next_id("RUN")
        _runs().insert_one({
            "_id": run_id, "scenario": scenario, "round": int(round), "seed": scn.get("seed"),
            "title": scn.get("title"), "status": "running", "steps_total": len(scn["steps"]),
            "steps_fired": 0, "started_at": ts, "ended_at": None,
        })
    return run_id


def end_run(run_id: str) -> None:
    cancel(run_id)
    _runs().update_one({"_id": run_id, "ended_at": None}, {"$set": {"ended_at": now()}})


def current_run() -> dict | None:
    """The newest run that hasn't ended (manual faults attach to it for the scorecard)."""
    return _runs().find_one({"ended_at": None}, sort=[("started_at", DESCENDING)])


def announce_current(run_id: str, round: int) -> None:
    """Mirror ``{_id:"current", run_id, round}`` into swarm.stats, exactly as the war room's
    POST /api/scenario does, so Blue can tag incidents without reading Red's data. Used by the
    CLI when it plays the operator's role; it is the only swarm write Red ever makes."""
    swarm()["stats"].update_one({"_id": "current"}, {"$set": {"run_id": run_id, "round": round, "ts": now()}},
                                upsert=True)


# ------------------------------------------------------------------ fire one fault
def fire(fault: str, target: str | None = None, params: dict | None = None, manual: bool = True,
         run_id: str | None = None, extra: dict | None = None, who: str | None = None,
         why: str | None = None, step: tuple[int, int] | None = None) -> dict:
    """Execute one fault and record it. Returns the final ``chaos_events`` document.

    The event is inserted *before* the fault executes (status ``firing``) so the war room
    can draw the lightning strike immediately; the outcome is written back with
    ``$set`` when it lands (agent_kill may wait up to 30 s for its victim).
    ``extra`` adds attribution fields to the event (the Red fleet sets ``by``/``red``); it can
    never overwrite the core fields.

    Every event says who, what and why, whatever fired it (the Runner is the one door for all chaos):
    ``source`` ("scenario" | "manual" | "red-fleet"), ``who`` (``scenario:<name>``, ``beekeeper`` or the
    attacker's id) and ``why`` (the attacker's reason, else what the fault tests).
    """
    f = faults.get(fault)
    target = faults.resolve_target(f, target)
    params = f.params_with_defaults(params)
    run = _runs().find_one({"_id": run_id}) if run_id else current_run()
    extra = extra or {}
    red = str(extra.get("by") or "").startswith("red-")
    if red and decider.enabled() and not extra.get("runner_decision"):   # RUNNER_DECIDES=1: admit every Red fire
        planner = (extra.get("red") or {}).get("campaign") and "commander"
        decision = decider.admit(f.name, target, params, extra["by"], source=planner or "striker")
        if decision["decision"] != "execute":
            return decider.refused(decision, f.name, target, params)
        extra = {**extra, "runner_decision": decision["_id"]}
    source = "red-fleet" if red else ("manual" if manual else "scenario")
    who = who or extra.get("by") or ("beekeeper" if manual else f"scenario:{(run or {}).get('scenario') or '?'}")
    why = why or ((extra.get("red") or {}).get("reason") if red else None) or f.tests
    event = {
        "_id": _next_id("CHX"), "fault": f.name, "kind": f.kind, "target": target, "params": params,
        "ts": now(), "scenario": run and run.get("scenario"), "run_id": run and run["_id"],
        "round": run and run.get("round"), "seed": run and run.get("seed"), "manual": manual,
        "status": "firing", "result": None,
        "outcome": {"guard_caught": None, "recovery_s": None, "state": "awaiting Hive evidence"}, "source": source, "who": who, "why": why,
    }
    event = {**{k: v for k, v in extra.items() if k not in event}, **event}
    _events().insert_one(event)
    if event.get("runner_decision"):
        decider.link(event["runner_decision"], event["_id"])
    # PROGRESS_ETA (off = no-op): ETA from past runs on start, one duration record + done line at the end
    with progress.track("world", f"attack:{f.name}", target, "red" if red else "chaos", agent=who,
                        label=f"{'Red fleet' if red else 'Chaos'}: {f.name} on {target or 'hive'}",
                        step=step[0] if step else None, of=step[1] if step else None) as tracked:
        try:
            result = faults.execute(f, target, params)
        except Exception as e:  # never lose the event record because a fault blew up
            log.exception("fault %s failed", fault)
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        tracked.outcome = "ok" if result.get("ok") else "failed"
    finished = _events().find_one_and_update(
        {"_id": event["_id"]},
        {"$set": {"result": result, "status": "ok" if result.get("ok") else "failed", "done_at": now()}},
        return_document=ReturnDocument.AFTER)
    if os.environ.get("CHAOS_OUTCOMES", "").strip().lower() in ("1", "true", "yes", "on"):
        threading.Thread(target=_observe_later, args=(event["_id"],), daemon=True,
                         name="chaos-outcome").start()
    return finished


def _observe_later(event_id: str) -> None:
    """Bounded sampling for seeded/manual drills without a fleet; no decisions or extra attacks."""
    from .redfleet.learning import outcome
    for delay in (5, 10, 15, 30):
        time.sleep(delay)
        try:
            event = _events().find_one({"_id": event_id})
            if not event:
                return  # full reset erased this event
            observed = outcome(event)
            _events().update_one({"_id": event_id}, {"$set": {"outcome": observed}})
            if observed["recovery_s"] is not None:
                return
        except Exception:
            log.exception("outcome sampling failed")
            return


# ------------------------------------------------------------------ play a scenario
def _play(run_id: str, scn: dict, stop: threading.Event, on_event=None) -> None:
    t0 = time.monotonic()
    total = len(scn["steps"])
    try:
        for i, step in enumerate(scn["steps"], 1):
            delay = config.pace(step["t"]) - (time.monotonic() - t0)  # PACE=0 -> back-to-back, same order
            if delay > 0 and stop.wait(delay):
                break
            if stop.is_set():
                break
            if decider.enabled():
                decider.seeded_step(step, scn["_id"], run_id)
            ev = fire(step["fault"], step["target"], step.get("params"), manual=False, run_id=run_id,
                      who=f"scenario:{scn['_id']}", why=step.get("why"),
                      **({"step": (i, total)} if progress.enabled() else {}))
            _runs().update_one({"_id": run_id}, {"$inc": {"steps_fired": 1}})
            if on_event:
                on_event(ev)
        status = "cancelled" if stop.is_set() else "played"
        _runs().update_one({"_id": run_id}, {"$set": {"status": status, "played_at": now()}})
    except Exception as e:
        log.exception("scenario %s crashed", scn["_id"])
        _runs().update_one({"_id": run_id}, {"$set": {"status": "error", "error": str(e)}})
    finally:
        with _active_lock:
            _active.pop(run_id, None)


def run_scenario(name: str, round: int | None = None, block: bool = False, announce: bool = False,
                 on_event=None) -> str:
    """Start a run of ``name`` and play its steps at their ``t`` offsets (scaled by PACE).

    ``block=False`` returns the run id immediately and plays in a daemon thread.
    ``on_event(event_doc)`` is called after each fault (the CLI uses it to print).
    """
    scn = scenarios.get(name)
    run_id = start_run(name, round)
    if announce:
        announce_current(run_id, _runs().find_one({"_id": run_id})["round"])
    stop = threading.Event()
    if block:
        with _active_lock:
            _active[run_id] = (threading.current_thread(), stop)
        _play(run_id, scn, stop, on_event)
        return run_id
    t = threading.Thread(target=_play, args=(run_id, scn, stop, on_event), name=f"chaos-{run_id}", daemon=True)
    with _active_lock:
        _active[run_id] = (t, stop)
    t.start()
    return run_id


def cancel(run_id: str | None = None) -> list[str]:
    """Stop pending steps of one run (or all runs). Already-fired faults stay fired.
    Cancelling everything (a reset) also calls off manual kills still hunting for a victim."""
    if run_id is None:
        # Thaw first: memory cleanup must never prevent waking a stopped bee.
        for label, cleanup in [("thaw", lambda: faults.thaw_overdue(all_=True)),
                               ("cancel hunts", faults.cancel_pending_hunts),
                               ("clear planted memory", faults.clear_planted_memory)]:
            try:
                cleanup()
            except Exception:
                log.exception("%s on reset failed", label)
    with _active_lock:
        targets = [run_id] if run_id else list(_active)
        for rid in targets:
            if rid in _active:
                _active[rid][1].set()
    return targets


def wait(run_id: str, timeout: float | None = None) -> bool:
    """Block until a background run has fired all its steps. True if finished."""
    with _active_lock:
        entry = _active.get(run_id)
    if entry is None or entry[0] is threading.current_thread():
        return True
    entry[0].join(timeout)
    return not entry[0].is_alive()


def refresh_outcomes(limit: int = 12) -> list[dict]:
    """Read-only Hive evidence; Runner records observations, never interprets them with a model.
    Called by the opt-in Strikers' OBSERVE so seeded/manual attacks also acquire outcomes.
    """
    from .redfleet.learning import outcome
    events = list(_events().find({"status": {"$in": ["ok", "failed"]}, "kind": {"$ne": "decision"}}).sort("ts", -1).limit(limit))
    for event in events:
        observed = outcome(event)
        _events().update_one({"_id": event["_id"]}, {"$set": {"outcome": observed}})
        event["outcome"] = observed
    return events
