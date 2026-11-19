"""Demo autoplay (opt-in: ``DEMO_AUTOPLAY=1``; off by default).

Cuts the presenter's clicks from ~8 to 3 (Night 1, the first Approve, Kill):

* **Night 1 → Night 2**: ``AUTOPLAY_GAP_S`` after Night 1's incident resolves and the estate is green, it does
  the ``next_round`` reset (playbooks kept) and starts ``bank-night-2``.
* **Night 2 → Night 3**: once Night 2 has resolved *and* the Upgrader's upgrade is ``proven``, it starts
  ``bank-night-3`` (no reset, like the presenter flow).
* **Approvals**: the first approval of a demo stays manual (Night 1's ``restart_batch``, a design-doc beat).
  Every later approval is auto-approved ``AUTOPLAY_APPROVE_S`` after it appears, with a visible line on the
  activity stream ("auto-approved (same as last time)"), ``decided_by: "autoplay"``.
* The agent kill stays a manual button press.

Everything goes through the same MongoDB writes as the buttons: the conditional approval update, the war-room
reset and the chaos runner. Advancing is claimed with a conditional update on ``swarm.stats{_id:"current"}``
(``autoplayed_run``), so a run is advanced at most once even across a war-room restart.
"""
from __future__ import annotations

import logging
import threading

from common import config
from common.db import now, swarm, world
from common.events import think

log = logging.getLogger("console.autoplay")

AGENT, ROLE = "autoplay", "beekeeper"
NEXT = {"bank-night-1": "bank-night-2", "bank-night-2": "bank-night-3"}
RESET_BEFORE = {"bank-night-2"}  # next_round reset before these (the hive keeps its playbooks)


def _age_s(ts) -> float:
    return (now() - ts).total_seconds() if ts else 0.0


class Autoplay:
    def __init__(self, gap_s: float | None = None, approve_s: float | None = None, poll_s: float = 0.5):
        self.gap_s = config.AUTOPLAY_GAP_S if gap_s is None else gap_s
        self.approve_s = config.AUTOPLAY_APPROVE_S if approve_s is None else approve_s
        self.poll_s = poll_s
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    # ------------------------------------------------------------------ approvals
    def approve_pending(self) -> list[str]:
        s = swarm()
        done = []
        for apr in s.approvals.find({"status": "pending"}).sort("ts", 1):
            if _age_s(apr.get("ts")) < self.approve_s:
                continue
            # the first approval of the demo stays the Beekeeper's own click
            if not s.approvals.find_one({"status": "approved", "_id": {"$ne": apr["_id"]}}, {"_id": 1}):
                continue
            res = s.approvals.update_one({"_id": apr["_id"], "status": "pending"},
                                         {"$set": {"status": "approved", "decided_by": "autoplay",
                                                   "decided_at": now()}})
            if not res.modified_count:
                continue
            same = apr.get("kind") == "step" and s.approvals.find_one(
                {"_id": {"$ne": apr["_id"]}, "kind": "step", "status": "approved", "desc": apr.get("desc")}, {"_id": 1})
            why = "same as last time" if same else "demo autoplay"
            think(AGENT, ROLE, f"Beekeeper: {apr['_id']} auto-approved ({why}) — {apr.get('desc') or apr.get('kind')}.",
                  incident=apr.get("incident"), system=apr.get("system"), kind="approval")
            log.info("auto-approved %s (%s)", apr["_id"], why)
            done.append(apr["_id"])
        return done

    # ------------------------------------------------------------------ nights
    def ready_to_advance(self, cur: dict) -> bool:
        s, w = swarm(), world()
        run_id, scenario = cur.get("run_id"), cur.get("scenario")
        if not run_id or scenario not in NEXT or cur.get("autoplayed_run") == run_id:
            return False
        incs = list(s.incidents.find({"run_id": run_id}, {"status": 1, "active": 1, "closed_at": 1}))
        if not incs or any(i.get("active") or i.get("status") != "resolved" for i in incs):
            return False
        if s.incidents.count_documents({"active": True}):
            return False
        if any(d.get("status") != "up" for d in w.systems.find({}, {"status": 1})):
            return False
        last_closed = max((i.get("closed_at") for i in incs if i.get("closed_at")), default=None)
        if _age_s(last_closed) < self.gap_s:
            return False
        if NEXT[scenario] == "bank-night-3":  # night 3 replays the attack after the Upgrader's upgrade
            upg = s.upgrade_requests.find_one({"system": "mainframe", "fault_class": "batch_abort"},
                                              sort=[("ts", -1)])
            if not upg or upg.get("status") != "proven" or _age_s(upg.get("proven_at")) < self.gap_s:
                return False
        return True

    def advance(self, cur: dict) -> str | None:
        from . import api  # noqa: PLC0415  (avoid an import cycle at module load)

        nxt = NEXT[cur["scenario"]]
        claimed = swarm().stats.find_one_and_update(
            {"_id": "current", "run_id": cur["run_id"], "autoplayed_run": {"$ne": cur["run_id"]}},
            {"$set": {"autoplayed_run": cur["run_id"], "autoplayed_at": now()}})
        if claimed is None:
            return None
        think(AGENT, ROLE, f"Autoplay: {cur['scenario']} is green — starting {nxt} in a moment.", kind="action")
        if nxt in RESET_BEFORE:
            api.reset({"mode": "next_round"})
        out = api.start_scenario(nxt)
        log.info("autoplay: %s -> %s (%s)", cur["scenario"], nxt, out.get("run_id"))
        return nxt

    def tick(self) -> None:
        self.approve_pending()
        cur = swarm().stats.find_one({"_id": "current"}) or {}
        if self.ready_to_advance(cur):
            self.advance(cur)

    # ------------------------------------------------------------------ thread
    def run(self) -> None:
        log.info("demo autoplay on (gap %ss, auto-approve after %ss)", self.gap_s, self.approve_s)
        while not self.stop.wait(self.poll_s):
            try:
                self.tick()
            except Exception:  # never take the war room down
                log.exception("autoplay tick failed")

    def start(self) -> "Autoplay":
        self.thread = threading.Thread(target=self.run, name="autoplay", daemon=True)
        self.thread.start()
        return self


def start_if_enabled() -> Autoplay | None:
    return Autoplay().start() if config.DEMO_AUTOPLAY else None
