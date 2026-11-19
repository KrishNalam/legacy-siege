"""Swarm supervisor:  python -m agents.supervisor

Spawns one OS process per agent (`python -m agents.agent <role> <id>`). It does no hive work itself and
never touches leases: when a child dies (e.g. Chaos SIGKILL), recovery of its WORK is the hive's job
(Undertaker + Recovery via MongoDB). The supervisor only refills the bench: after RECOVERY_RESPAWN_S it starts
a fresh recovery standby in the dead bee's place. Singleton roles with no standby (verifier, monitor, reporter)
and coordinators come back in their own role. SIGTERM/SIGINT stops all children cleanly.

Env AGENTS_ROSTER="role:id,role:id" overrides the roster (tests).

AGENTS_AUTOSCALE=1 (default off) switches to dynamic scaling (see agents.scale): the standing roster shrinks to
DYNAMIC_ROSTER (coordinators, verifier, reporter, monitor, warm recovery agents) and investigators/operators are started on request from
swarm.spawn_requests, out of AGENTS_WARM pre-imported spare processes, up to AGENTS_MAX_AGENTS live agents.
Spawned bees retire themselves (exit 0, agent doc deleted) and are not respawned; a spawned bee that DIES
(SIGKILL, crash) is replaced on the bench by a recovery exactly like a roster bee. Live counts go to
swarm.stats{_id:"swarm_scale"} for the war room.
"""
from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from common import config

ROOT = Path(__file__).resolve().parent.parent
ROSTER = [
    ("coordinator", "coordinator-1"), ("coordinator", "coordinator-2"),
    ("investigator", "investigator-1"), ("investigator", "investigator-2"),
    ("operator", "operator-mainframe-1"), ("operator", "operator-unix-1"), ("operator", "operator-web-1"),
    ("verifier", "verifier-1"), ("reporter", "reporter-1"), ("monitor", "monitor-1"),
    ("recovery", "recovery-1"), ("recovery", "recovery-2"),
]
# dynamic mode: no standing investigators or operators; recovery agents stay warm so a kill is always taken over at once
DYNAMIC_ROSTER = [
    ("coordinator", "coordinator-1"), ("coordinator", "coordinator-2"),
    ("verifier", "verifier-1"), ("reporter", "reporter-1"), ("monitor", "monitor-1"),
    ("recovery", "recovery-1"), ("recovery", "recovery-2"),
]
SELF_RESPAWN = {"verifier", "monitor", "reporter", "coordinator", "recovery"}


def dynamic() -> bool:
    from agents import scale  # noqa: PLC0415
    return scale.enabled()


def roster() -> list[tuple[str, str]]:
    raw = os.environ.get("AGENTS_ROSTER", "").strip()
    if not raw:
        from upgrader.help import enabled as upgrader_help  # structure v3: Monitor #2 is its own agent
        from agents.toolsmith import enabled as toolsmith_on  # toolsmith engineers: Monitor-type, subtype toolsmith
        return (list(DYNAMIC_ROSTER if dynamic() else ROSTER) + ([("monitor", "monitor-2")] if upgrader_help() else [])
                + ([("monitor", "toolsmith-1")] if toolsmith_on() else []))
    return [tuple(x.split(":", 1)) for x in raw.split(",") if ":" in x]  # type: ignore[misc]


def _load_prctl():
    try:
        import ctypes  # noqa: PLC0415
        return ctypes.CDLL("libc.so.6", use_errno=True).prctl
    except Exception:
        return None


_PRCTL = _load_prctl() if sys.platform.startswith("linux") else None  # resolved in the parent, not after fork
_SUPERVISOR_PID = os.getpid()


def _die_with_parent() -> None:
    """preexec_fn for every agent process: Linux sends it SIGTERM when the supervisor dies (crash, SIGKILL),
    so a supervisor restart never leaves a second set of agents behind (they shut down cleanly: leases are
    handed back and the hive recovers their work from MongoDB as after any death). No imports here: the
    MongoDB client's threads may hold locks at fork time."""
    _PRCTL(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    if os.getppid() != _SUPERVISOR_PID:  # the supervisor already died between fork and prctl
        os._exit(0)


_PREEXEC = _die_with_parent if _PRCTL is not None else None


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} [supervisor] {msg}", flush=True)


class Supervisor:
    def __init__(self):
        self.children: dict[str, tuple[str, subprocess.Popen]] = {}
        self.pending: list[tuple[float, str, str, str | None]] = []  # (when, role, id, standby_for)
        self.stopping = False
        self.next_recovery = 1 + sum(1 for r, _ in roster() if r == "recovery")
        self.retired: set[str] = set()   # extra recovery agents stopped on a roster restore: never respawned
        self.restore_seen = None
        self.restore_checked = 0.0
        self.nights = 0                  # roster restores so far
        self.bench_born: dict[str, int] = {}  # replacement recovery -> self.nights when it was started
        # dynamic scaling
        self.dynamic = dynamic()
        self.warm: list[subprocess.Popen] = []      # pre-imported spares waiting for an assignment on stdin
        self.ephemeral: set[str] = set()            # agent ids started on request (they retire themselves)
        self.requests_checked = 0.0
        self.warm_started = 0.0
        self.scale = {"spawned": 0, "retired": 0, "peak": 0, "cold": 0, "spawn_ms": []}
        self.scale_written = None

    def spawn(self, role: str, agent_id: str, standby_for: str | None = None, ephemeral: bool = False) -> bool:
        args = [role, agent_id] + (["--standby-for", standby_for] if standby_for else []) + \
            (["--ephemeral"] if ephemeral else [])
        p, how = None, "cold"
        while self.dynamic and self.warm and p is None:
            w = self.warm.pop(0)
            if w.poll() is not None:
                continue
            try:
                w.stdin.write((shlex.join(args) + "\n").encode())
                w.stdin.close()
                p, how = w, "warm"
            except (BrokenPipeError, OSError):
                continue
        if p is None:
            p = subprocess.Popen([sys.executable, "-m", "agents.agent", *args], cwd=ROOT, preexec_fn=_PREEXEC)
        self.children[agent_id] = (role, p)
        if standby_for:
            self.bench_born[agent_id] = self.nights
        if ephemeral:
            self.ephemeral.add(agent_id)
        log(f"spawned {agent_id} ({role}) pid={p.pid}" + (f" standby for {standby_for}" if standby_for else "")
            + (f" [{how}{', on request' if ephemeral else ''}]" if self.dynamic else ""))
        return how == "warm"

    # ------------------------------------------------------------------ dynamic scaling
    def top_up_warm(self) -> None:
        from agents import scale  # noqa: PLC0415
        self.warm = [w for w in self.warm if w.poll() is None]
        # at most one new spare per 0.5 s: a spare that keeps dying at import must not become a fork loop
        if len(self.warm) < scale.WARM and time.monotonic() - self.warm_started >= 0.5:
            self.warm_started = time.monotonic()
            self.warm.append(subprocess.Popen([sys.executable, "-m", "agents.agent", "--warm"], cwd=ROOT,
                                              stdin=subprocess.PIPE, preexec_fn=_PREEXEC))

    def live_count(self) -> int:
        return sum(1 for _r, p in self.children.values() if p.poll() is None)

    def free_id(self, prefix: str) -> str:
        """Lowest free `<prefix>-<n>`: not a live child and not in swarm.agents (a dead bee keeps its id;
        a retired one deleted its doc, so its number is reused)."""
        from common.db import col  # noqa: PLC0415
        taken = set(self.children) | {d["_id"] for d in col("agents").find(
            {"_id": {"$regex": f"^{re.escape(prefix)}-[0-9]+$"}}, {"_id": 1})}
        n = 1
        while f"{prefix}-{n}" in taken:
            n += 1
        return f"{prefix}-{n}"

    def serve_requests(self) -> None:
        if time.monotonic() - self.requests_checked < 0.25:
            return
        self.requests_checked = time.monotonic()
        try:  # a MongoDB blip must never take the supervisor (and every agent with it) down
            self._serve_requests()
        except Exception as e:
            log(f"spawn requests: {type(e).__name__}: {e}")

    def _serve_requests(self) -> None:
        from common.db import col, now  # noqa: PLC0415
        from agents import scale  # noqa: PLC0415
        try:
            reqs = list(col("spawn_requests").find({"status": "pending"}).sort("ts", 1).limit(20))
        except Exception:
            return
        for r in reqs:
            age = (now() - r["ts"]).total_seconds()
            note = None
            if r.get("task"):
                t = col("tasks").find_one({"_id": r["task"]}, {"status": 1})
                if not t or t.get("status") != "open":
                    note = "work already taken"
            elif r.get("incident"):
                i = col("incidents").find_one({"_id": r["incident"]}, {"status": 1, "active": 1})
                if not i or not i.get("active") or i.get("status") != "open":
                    note = "incident already claimed"
            if note is None and age > scale.REQUEST_TTL_S:
                note = f"not served within {scale.REQUEST_TTL_S:.0f}s"
            if note:
                col("spawn_requests").update_one({"_id": r["_id"], "status": "pending"},
                                                 {"$set": {"status": "cancelled", "note": note}})
                continue
            if self.live_count() >= scale.MAX_AGENTS:
                if not r.get("note"):
                    col("spawn_requests").update_one({"_id": r["_id"]}, {"$set": {
                        "note": f"deferred: at the AGENTS_MAX_AGENTS ceiling ({scale.MAX_AGENTS})"}})
                    log(f"{r['_id']} deferred: {scale.MAX_AGENTS} live agents (ceiling)")
                continue
            role = r["role"]
            if role not in ("investigator", "operator", "recovery") or (role == "operator" and not r.get("specialty")):
                col("spawn_requests").update_one({"_id": r["_id"]}, {"$set": {"status": "cancelled",
                                                                             "note": "unsupported request"}})
                continue
            prefix = f"operator-{r['specialty']}" if role == "operator" else role
            agent_id = self.free_id(prefix)
            t0 = time.monotonic()
            warm = self.spawn(role, agent_id, ephemeral=True)
            ms = round((time.monotonic() - t0) * 1000, 1)
            _role, p = self.children[agent_id]
            col("spawn_requests").update_one({"_id": r["_id"]}, {"$set": {
                "status": "spawned", "agent_id": agent_id, "pid": p.pid, "warm": warm, "spawned_at": now(),
                "spawn_ms": ms}})
            if role == "investigator" and r.get("incident"):  # hand the incident straight to the new investigator
                col("incidents").update_one({"_id": r["incident"], "status": "open", "assigned_to": None},
                                            {"$set": {"assigned_to": agent_id, "assigned_by": r.get("requested_by"),
                                                      "assigned_at": now()}})
            self.scale["spawned"] += 1
            self.scale["cold"] += 0 if warm else 1
            self.scale["spawn_ms"] = (self.scale["spawn_ms"] + [ms])[-50:]
        self.write_scale()

    def write_scale(self) -> None:
        from common.db import col, now  # noqa: PLC0415
        from agents import scale  # noqa: PLC0415
        live = self.live_count()
        self.scale["peak"] = max(self.scale["peak"], live)
        doc = {"dynamic": True, "live": live, "spawned_live": sum(1 for a in self.ephemeral if a in self.children),
               "peak": self.scale["peak"], "spawned": self.scale["spawned"], "retired": self.scale["retired"],
               "cold_spawns": self.scale["cold"], "warm_ready": len([w for w in self.warm if w.poll() is None]),
               "max": scale.MAX_AGENTS, "base": len(roster()),
               "avg_spawn_ms": round(sum(self.scale["spawn_ms"]) / len(self.scale["spawn_ms"]), 1)
               if self.scale["spawn_ms"] else None}
        if doc == self.scale_written:
            return
        try:
            col("stats").update_one({"_id": "swarm_scale"}, {"$set": {**doc, "updated_at": now()}}, upsert=True)
            self.scale_written = doc
        except Exception:
            pass

    def reap(self) -> None:
        for agent_id, (role, p) in list(self.children.items()):
            code = p.poll()
            if code is None:
                continue
            del self.children[agent_id]
            if self.stopping:
                continue
            if agent_id in self.retired:  # retired on a roster restore: remove its bee from the hive
                self.retired.discard(agent_id)
                self.ephemeral.discard(agent_id)
                try:
                    from common.db import col  # noqa: PLC0415
                    col("agents").delete_one({"_id": agent_id, "pid": p.pid})
                except Exception:
                    pass
                continue
            if agent_id in self.ephemeral:
                self.ephemeral.discard(agent_id)
                if code == 0:  # retired itself (dynamic scaling): gone for good, doc already deleted
                    self.scale["retired"] += 1
                    try:
                        from common.db import col  # noqa: PLC0415
                        col("agents").delete_one({"_id": agent_id, "pid": p.pid})
                    except Exception:
                        pass
                    log(f"{agent_id} retired")
                    continue
            when = time.monotonic() + config.RECOVERY_RESPAWN_S
            if role in SELF_RESPAWN:
                self.pending.append((when, role, agent_id, None))
                log(f"{agent_id} exited ({code}); respawning in its own role in {config.RECOVERY_RESPAWN_S:.0f}s")
            else:
                nid = f"recovery-{self.next_recovery}"
                self.next_recovery += 1
                self.pending.append((when, "recovery", nid, agent_id))
                log(f"{agent_id} exited ({code}); {nid} joins the bench in {config.RECOVERY_RESPAWN_S:.0f}s")

    def respawn_due(self) -> None:
        now = time.monotonic()
        due = [x for x in self.pending if x[0] <= now]
        self.pending = [x for x in self.pending if x[0] > now]
        for _, role, agent_id, standby_for in due:
            self.spawn(role, agent_id, standby_for)

    def restore_roster(self) -> None:
        """A full demo reset (war room) writes swarm.stats{_id:"roster_reset"}: bring the bench back to the
        original roster — respawn missing roster bees in their own role, retire extra respawned recovery agents."""
        if time.monotonic() - self.restore_checked < 2.0:
            return
        self.restore_checked = time.monotonic()
        try:
            from common.db import col  # noqa: PLC0415
            doc = col("stats").find_one({"_id": "roster_reset"})
        except Exception:
            return
        if not doc or doc.get("at") == self.restore_seen:
            return
        self.restore_seen = doc.get("at")
        if self.dynamic:  # a new night: per-night scale counters start over; old requests are void
            self.scale.update(spawned=0, retired=0, peak=0, cold=0, spawn_ms=[])
            self.scale_written = None  # a full reset wiped swarm.stats: write the doc again
            try:
                col("spawn_requests").update_many({"status": "pending"},
                                                  {"$set": {"status": "cancelled", "note": "reset"}})
            except Exception:
                pass
        base = roster()
        base_ids = {i for _, i in base}
        # A bee that died this night is still owed its replacement recovery on the bench: start it now instead of
        # dropping it (at stage pace the next night's reset lands before RECOVERY_RESPAWN_S, and the dead bee's
        # roster slot is refilled in its own role below). Replacement recovery agents of the night now ending survive
        # this reset (demo_check / the war room still look for them) and retire at the next one.
        owed = [(role, aid, sb) for _, role, aid, sb in self.pending if sb]
        self.pending = []
        for agent_id, (_role, p) in list(self.children.items()):
            if agent_id in self.bench_born and self.bench_born[agent_id] == self.nights:
                continue  # replacement for a bee that died in the night now ending: keep it one more night
            if agent_id not in base_ids and p.poll() is None:
                self.bench_born.pop(agent_id, None)
                self.retired.add(agent_id)
                p.send_signal(signal.SIGTERM)
                log(f"roster restore: retiring {agent_id}")
        for role, agent_id in base:
            if agent_id not in self.children:
                self.spawn(role, agent_id)
        for role, agent_id, standby_for in owed:
            if agent_id not in self.children:
                self.spawn(role, agent_id, standby_for)
        self.nights += 1
        try:
            col("stats").delete_one({"_id": "roster_reset", "at": self.restore_seen})
        except Exception:
            pass

    def stop(self, *_, code: int = 0):
        if self.stopping:
            return
        self.stopping = True
        log("stopping all agents")
        procs = [p for _, p in self.children.values()] + list(self.warm)  # roster, spawned and warm spares
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 8
        for p in procs:
            try:
                p.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(code)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        try:
            self._run()
        except BaseException as e:  # never orphan the agents: an unexpected error stops them, then exits
            if not isinstance(e, SystemExit):
                log(f"supervisor error: {type(e).__name__}: {e}; stopping all agents")
            self.stop(code=1)
            raise

    def _run(self) -> None:
        for role, agent_id in roster():
            self.spawn(role, agent_id)
        if self.dynamic:
            log(f"dynamic scaling ON: base roster {len(roster())}, warm spares, ceiling {self._max()} live agents")
        while True:
            if self.dynamic:
                self.top_up_warm()
                time.sleep(0.1)
                self.serve_requests()
            else:
                time.sleep(0.5)
            self.reap()
            self.respawn_due()
            self.restore_roster()

    @staticmethod
    def _max() -> int:
        from agents import scale  # noqa: PLC0415
        return scale.MAX_AGENTS


def main() -> None:
    Supervisor().run()


if __name__ == "__main__":
    main()
