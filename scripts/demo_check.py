"""End-to-end demo rehearsal: drives the war-room API like a presenter and checks every beat in MongoDB.

    .venv/bin/python -m scripts.demo_check                 # full reset, nights 1-3, Gardener, reset again
    .venv/bin/python -m scripts.demo_check --no-reset      # start from the current state
    .venv/bin/python -m scripts.demo_check --keep          # leave the upgraded estate (no final reset)
    .venv/bin/python -m scripts.demo_check --base http://127.0.0.1:8700 --approve-delay 2

The stack must already be running (`.venv/bin/python run.py --reset`). Approvals are auto-approved through
POST /api/approvals/{id} (like the Beekeeper clicking Approve); the agent kill is fired through POST /api/kill
right after restart_batch is approved, so the worker dies in phase step_sent. Exit code 0 = every beat PASS.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # also runnable as a plain script
from common import config  # noqa: E402
from common.config import env_float  # noqa: E402
from common.db import swarm, world  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAINFRAME_PY = ROOT / "systems" / "mainframe.py"
CHAIN_PATH = ["portal", "unix", "mainframe"]


# ---------------------------------------------------------------------------------------------- output
class Report:
    def __init__(self):
        self.rows: list[tuple[str, bool, float | None, str]] = []
        self.t0 = time.monotonic()

    def add(self, beat: str, ok: bool, detail: str, secs: float | None = None) -> bool:
        self.rows.append((beat, bool(ok), secs, detail))
        mark = "PASS" if ok else "FAIL"
        t = f"{secs:6.1f}s" if secs is not None else "       "
        print(f"  [{mark}] {t}  {beat}: {detail}", flush=True)
        return ok

    def table(self) -> bool:
        w = max(len(r[0]) for r in self.rows) if self.rows else 10
        print("\n" + "=" * (w + 80))
        print(f"{'beat'.ljust(w)}  result  time     detail")
        print("-" * (w + 80))
        for beat, ok, secs, detail in self.rows:
            t = f"{secs:6.1f}s" if secs is not None else "    -  "
            print(f"{beat.ljust(w)}  {'PASS' if ok else 'FAIL'}    {t}  {detail[:110]}")
        print("-" * (w + 80))
        n_ok = sum(1 for r in self.rows if r[1])
        total = time.monotonic() - self.t0
        verdict = "ALL PASS" if n_ok == len(self.rows) else f"{len(self.rows) - n_ok} FAILED"
        print(f"{n_ok}/{len(self.rows)} checks passed — {verdict} — total {total:.0f}s")
        return n_ok == len(self.rows)


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')}  {msg}", flush=True)


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def worker_kill(ev: dict) -> bool:
    """A kill chaos event aimed at a worker (by role selector, or by a worker's agent id)."""
    sel = (ev.get("params") or {}).get("selector") or {}
    return sel.get("role") == "worker" or str(sel.get("agent") or "").startswith("worker")


# ---------------------------------------------------------------------------------------------- driver
class Demo:
    def __init__(self, base: str, approve_delay: float, report: Report, autoplay: bool = False,
                 red_kill: bool = False):
        self.http = httpx.Client(base_url=base, timeout=60)  # a full reset on Atlas can take ~10 s
        self.sys_http = httpx.Client(base_url=config.SYSTEMS_URL, timeout=10)
        self.approve_delay = approve_delay
        self.autoplay = autoplay  # DEMO_AUTOPLAY stack: click only Night 1, its approval and the kill
        self.red_kill = red_kill  # RED_FLEET stage run: the Red Assassin fires the night-1 kill, not POST /api/kill
        self.r = report
        self.s, self.w = swarm(), world()

    # ---- api
    def post(self, path: str, body: dict | None = None) -> dict:
        resp = self.http.post(path, json=body or {})
        resp.raise_for_status()
        return resp.json()

    def mf_state(self) -> dict:
        return self.sys_http.get("/mainframe/state").json()

    def approve(self, apr_id: str) -> None:
        try:
            self.post(f"/api/approvals/{apr_id}", {"decision": "approved"})
            log(f"approved {apr_id} via /api/approvals")
        except httpx.HTTPStatusError as e:
            log(f"approve {apr_id}: {e.response.status_code} {e.response.text}")

    # ---- waits
    def wait(self, what: str, fn, timeout: float, poll: float = 0.5):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            v = fn()
            if v:
                return v, time.monotonic() - t0
            time.sleep(poll)
        return None, time.monotonic() - t0

    def live_agents(self) -> list[dict]:
        cutoff = datetime.now(tz=timezone.utc).timestamp() - max(2 * config.HEARTBEAT_S, 6)
        return [a for a in self.s.agents.find({"status": {"$ne": "dead"}})
                if a.get("last_seen") and a["last_seen"].timestamp() >= cutoff]

    # ------------------------------------------------------------------------------------------ preflight
    def preflight(self, reset: bool) -> bool:
        try:
            self.http.get("/api/scorecard").raise_for_status()
            self.sys_http.get("/mainframe/health").raise_for_status()
        except Exception as e:
            return self.r.add("preflight", False, f"stack not reachable ({e}); start it with run.py")
        if reset:
            log("POST /api/reset {mode: full}")
            out = self.post("/api/reset", {"mode": "full"})
            gr = out.get("gardener_revert")
            if isinstance(gr, dict) and gr.get("changed"):
                log(f"gardener revert restored systems/mainframe.py from {Path(gr['backup']).name}")

        def ready():
            agents = self.live_agents()
            ids = {a["_id"] for a in agents}
            roles = [a["role"] for a in agents]
            systems = {d["_id"]: d for d in self.w.systems.find()}
            # SWARM_DYNAMIC: no standing scouts/workers (they are spawned per incident), so don't wait for them
            dyn = bool((self.s.stats.find_one({"_id": "swarm_scale"}) or {}).get("dynamic"))
            pool = dyn or (roles.count("worker") >= 3 and "worker-mainframe-1" in ids and roles.count("scout") >= 1)
            ok = (len(systems) == 3 and all(d["status"] == "up" for d in systems.values())
                  and pool and "guard-1" in ids
                  and "sentinel-1" in ids and "gardener-1" in ids
                  and roles.count("nurse") >= 1 and not self.s.incidents.count_documents({"active": True}))
            return (agents, systems) if ok else None

        got, secs = self.wait("ready", ready, 60)
        if not got:
            agents = sorted(a["_id"] for a in self.live_agents())
            return self.r.add("preflight", False, f"stack not ready after 60s; live agents={agents}", secs)
        agents, systems = got
        st = self.mf_state()
        ok = st.get("validation_enabled") is False and systems["mainframe"]["version"] == 1
        return self.r.add("preflight", ok, f"{len(agents)} live agents, systems up, mainframe v"
                          f"{systems['mainframe']['version']} legacy validate_record="
                          f"{'patched' if st.get('validation_enabled') else 'original'}", secs)

    # ------------------------------------------------------------------------------------------ a night
    def start(self, scenario: str) -> dict:
        """POST the scenario, or with autoplay wait for the war room to start it by itself."""
        if not self.autoplay or scenario == "bank-night-1":
            log(f"POST /api/scenario/{scenario}")
            return self.post(f"/api/scenario/{scenario}")
        cur, secs = self.wait("autoplay start", lambda: self.s.stats.find_one(
            {"_id": "current", "scenario": scenario}), 180)
        if not cur:
            raise RuntimeError(f"autoplay never started {scenario}")
        log(f"autoplay started {scenario} as {cur['run_id']} (waited {secs:.1f}s)")
        return cur

    def chain_only(self) -> dict:
        """With the Red fleet on, its exploits open their own security incidents: the cascade beats count chain ones."""
        return {"kind": {"$ne": "security"}} if self.red_kill else {}

    def night_incidents(self, obs: dict) -> list[dict]:
        """The night's incidents. Red mode: the chain incidents opened while the night's cascade incident was open
        (a Red re-strike after it closed is a new attack wave, not a failed coalesce)."""
        q = {"run_id": obs["run_id"], **self.chain_only()}
        first = self.s.incidents.find_one({"_id": obs.get("incident")}) if self.red_kill and obs.get("incident") else None
        if first and first.get("closed_at"):
            q["opened_at"] = {"$lte": first["closed_at"]}
        return list(self.s.incidents.find(q).sort("opened_at", 1))

    def red_worker_kill(self, run_id: str, obs: dict) -> dict | None:
        """The Red Assassin's first kill of a worker this night. Beat d is the worker kill + nurse takeover; the
        Assassin may also take out a queen first (the standby takes the crown), which is logged, not scored."""
        for ev in self.w.chaos_events.find({"run_id": run_id, "fault": "agent_kill", "by": "red-assassin",
                                            "_id": {"$nin": obs.setdefault("other_kills", [])}}).sort("ts", 1):
            if worker_kill(ev):
                return ev
            obs["other_kills"].append(ev["_id"])
            log(f"Red Assassin fired {ev['_id']} {ev.get('params', {}).get('selector')} (not a worker: a takeover, "
                "not the scored kill)")
        return None

    def play_night(self, scenario: str, kill: bool, timeout: float) -> dict:
        """Start a scenario and service it until its incident closes. Returns observations."""
        out = self.start(scenario)
        run_id, t0 = out["run_id"], time.monotonic()
        manual = not self.autoplay or scenario == "bank-night-1"  # autoplay approves the later ones itself
        obs = {"run_id": run_id, "round": out.get("round"), "cascade_at": None, "max_active": 0,
               "approvals": {}, "kill_fired_at": None, "kill_wall": None, "incident": None, "closed_after": None}
        approved: set[str] = set()
        while time.monotonic() - t0 < timeout:
            systems = {d["_id"]: d for d in self.w.systems.find()}
            mf, ux, po = systems.get("mainframe", {}), systems.get("unix", {}), systems.get("portal", {})
            if obs["cascade_at"] is None and po.get("status") == "degraded" and po.get("blames") == "unix" \
                    and ux.get("status") == "degraded" and ux.get("blames") == "mainframe" \
                    and mf.get("status") == "up" and (mf.get("metrics") or {}).get("batch_status") == "ABENDED":
                obs["cascade_at"] = time.monotonic() - t0
                log(f"cascade visible: portal→unix→mainframe (mainframe up, batch ABENDED) at {obs['cascade_at']:.1f}s")
            active = list(self.s.incidents.find({"active": True, **self.chain_only()}, {"_id": 1}))
            obs["max_active"] = max(obs["max_active"], len(active))
            for apr in self.s.approvals.find({"status": "pending", "kind": "step"}):
                a = obs["approvals"].setdefault(apr["_id"], {"seen_at": time.monotonic(), "doc": apr})
                led = self.s.ledger.find_one({"action_id": apr.get("action_id")})
                a["ledger_before_approval"] = led is not None and led.get("status") != "planned"
                if manual and time.monotonic() - a["seen_at"] >= self.approve_delay and apr["_id"] not in approved:
                    approved.add(apr["_id"])
                    self.approve(apr["_id"])
                    step = apr.get("step_id", "")
                    if kill and obs["kill_fired_at"] is None and "restart_batch" in apr.get("desc", "") \
                            and not (self.red_kill and self.red_worker_kill(run_id, obs)):
                        if self.red_kill:  # the Assassin spent its strike elsewhere (e.g. a queen): press Kill
                            log(f"no Red worker kill yet (other kills: {obs.get('other_kills') or []}); "
                                "the presenter presses Kill")
                            obs["kill_source"] = "manual"
                        log(f"POST /api/kill {{role: worker, phase: step_sent}} (restart_batch {step} is next)")
                        self.post("/api/kill", {"selector": {"role": "worker", "phase": "step_sent"}})
                        obs["kill_fired_at"] = time.monotonic() - t0
                        obs["kill_wall"] = datetime.now(tz=timezone.utc)
            if kill and self.red_kill and obs["kill_fired_at"] is None:  # the Red Assassin presses Kill itself
                ev = self.red_worker_kill(run_id, obs)
                if ev:
                    log(f"Red Assassin fired {ev['_id']} {ev.get('params', {}).get('selector')}")
                    obs["kill_fired_at"] = time.monotonic() - t0
                    obs["kill_wall"] = ev["ts"]
                    obs["kill_event"] = ev["_id"]
            inc = self.s.incidents.find_one({"run_id": run_id, **self.chain_only()}, sort=[("opened_at", 1)])
            if inc:
                obs["incident"] = inc["_id"]
                if inc["status"] in ("resolved", "failed") and not inc.get("active"):
                    obs["closed_after"] = time.monotonic() - t0
                    # autoplay may start the next night while this one's checks run: freeze the estate now
                    obs["systems_at_close"] = {d["_id"]: d["status"] for d in self.w.systems.find()}
                    log(f"{inc['_id']} {inc['status']} after {obs['closed_after']:.1f}s")
                    break
            time.sleep(0.5)
        obs["elapsed"] = time.monotonic() - t0
        return obs

    # ------------------------------------------------------------------------------------------ beats
    def night1(self, timeout: float) -> dict | None:
        log("=== NIGHT 1: cascade, lying system, approval, agent kill ===")
        obs = self.play_night("bank-night-1", kill=True, timeout=timeout)
        run_id = obs["run_id"]
        incs = self.night_incidents(obs)
        inc = incs[0] if incs else None
        # (a) cascade + coalescing + diagnosis
        self.r.add("a. cascade visible in world.systems", obs["cascade_at"] is not None,
                   "portal degraded→blames unix, unix degraded→blames mainframe, mainframe up + batch ABENDED"
                   if obs["cascade_at"] is not None else "never saw the full cascade", obs["cascade_at"])
        self.r.add("a. exactly one coalesced incident", len(incs) == 1 and obs["max_active"] == 1,
                   f"{len(incs)} incident(s) for {run_id}, max {obs['max_active']} active at once; "
                   f"affected={inc.get('affected') if inc else None} key={inc.get('correlation_key') if inc else None}")
        if not inc:
            return None
        self.r.add("a. diagnosis walk portal→unix→mainframe",
                   inc.get("diagnosis_path") == CHAIN_PATH and inc.get("root_cause_system") == "mainframe"
                   and inc.get("fault_class") == "batch_abort",
                   f"path={inc.get('diagnosis_path')} root={inc.get('root_cause_system')} class={inc.get('fault_class')} "
                   f"diagnosis_s={(inc.get('metrics') or {}).get('diagnosis_s')}")
        self.check_lie(inc)
        self.check_approval(inc, obs)
        self.check_kill(inc, obs)
        self.check_resolved(inc, obs, "e.")
        return {"obs": obs, "inc": self.s.incidents.find_one({"_id": inc["_id"]})}

    def check_lie(self, inc: dict) -> None:
        inc_id = inc["_id"]
        rejects = list(self.s.thoughts.find({"incident": inc_id, "kind": "reject", "agent": "guard-1"}))
        lied_steps = [s for s in inc.get("steps") or [] if len(s.get("attempts") or []) >= 2 and s["system"] == "mainframe"]
        detail, ok = "no mainframe step needed a second attempt", False
        if lied_steps:
            st = lied_steps[0]
            rows = {r["action_id"]: r for r in self.s.ledger.find({"step_id": st["step_id"]})}
            first, last = st["attempts"][0], st["attempts"][-1]
            r1, rn = rows.get(first, {}), rows.get(last, {})
            receipt = self.w.op_receipts.find_one({"_id": first}) or {}
            ok = (bool(rejects) and r1.get("status") == "failed"
                  and (r1.get("system_response") or {}).get("applied") is True
                  and rn.get("status") == "verified" and bool(rn.get("evidence")) and st["status"] == "verified"
                  and len(set(st["attempts"])) == len(st["attempts"]))
            detail = (f"{st['op']}: {first} ACK applied:true → ledger failed (receipt lied={receipt.get('lied')}); "
                      f"{last} verified, evidence '{(rn.get('evidence') or '')[:50]}'; guard reject: "
                      f"'{rejects[0]['text'][:40] if rejects else 'NONE'}'")
        self.r.add("b. lying system caught (ACK ≠ APPLIED)", ok, detail)
        # every step's attempts == its ledger rows, and the current attempt's ledger status == step status
        mismatches = []
        for st in inc.get("steps") or []:
            rows = {r["action_id"]: r for r in self.s.ledger.find({"step_id": st["step_id"]})}
            if set(rows) != set(st.get("attempts") or []):
                mismatches.append(f"{st['step_id']} attempts {st.get('attempts')} vs ledger {sorted(rows)}")
            elif st.get("action_id") and rows[st["action_id"]]["status"] != st["status"]:
                mismatches.append(f"{st['step_id']} step {st['status']} vs ledger {rows[st['action_id']]['status']}")
        n_rows = self.s.ledger.count_documents({"incident": inc_id})
        n_ids = len(self.s.ledger.distinct("action_id", {"incident": inc_id}))
        self.r.add("b. ledger ⇄ incident steps in sync", not mismatches and n_rows == n_ids,
                   "; ".join(mismatches) or f"{n_rows} ledger receipts, all action_ids unique, statuses match steps")

    def check_approval(self, inc: dict, obs: dict) -> None:
        inc_id = inc["_id"]
        step = next((s for s in inc.get("steps") or [] if s["op"] == "restart_batch"), None)
        apr = self.s.approvals.find_one({"incident": inc_id, "step_id": step["step_id"]}) if step else None
        if not (step and apr):
            self.r.add("c. approval before restart_batch", False, "no approval doc for restart_batch")
            return
        first = (step.get("attempts") or [None])[0]
        led = self.s.ledger.find_one({"action_id": first}) or {}
        ok = (apr["status"] == "approved" and apr.get("decided_by") == "beekeeper" and led.get("ts") is not None
              and apr["ts"] <= led["ts"] and apr["decided_at"] <= led["ts"]
              and not obs["approvals"].get(apr["_id"], {}).get("ledger_before_approval")
              and led.get("approver") == "beekeeper")
        lead = (led["ts"] - apr["decided_at"]).total_seconds() if led.get("ts") and apr.get("decided_at") else None
        self.r.add("c. approval before restart_batch", ok,
                   f"{apr['_id']} pending at {apr['ts']:%H:%M:%S}, approved via API, op sent "
                   f"{lead:.1f}s after decision; ledger approver={led.get('approver')}" if lead is not None
                   else f"{apr['_id']} status={apr['status']} ledger={led.get('status')}")

    def check_kill(self, inc: dict, obs: dict) -> None:
        if obs["kill_fired_at"] is None:
            self.r.add("d. agent kill → nurse takeover", False, "kill never fired (no restart_batch approval)")
            return
        q = {"_id": obs["kill_event"]} if obs.get("kill_event") else \
            {"run_id": obs["run_id"], "fault": "agent_kill", "source": "manual"} if obs.get("kill_source") else \
            {"run_id": obs["run_id"], "fault": "agent_kill"}
        ev, _ = self.wait("kill event", lambda: self.w.chaos_events.find_one(
            {**q, "status": {"$in": ["ok", "failed"]}}), 40)
        res = (ev or {}).get("result") or {}
        if not res.get("ok"):
            self.r.add("d. worker SIGKILLed in step_sent", False, f"kill event: {res}")
            return
        victim, pid = res["agent"], res["pid"]
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        self.r.add("d. worker SIGKILLed in step_sent", not alive and res.get("phase") == "step_sent",
                   f"{victim} pid {pid} phase={res.get('phase')} task={res.get('task')} matched={res.get('matched')} "
                   f"waited {res.get('waited_s')}s; process {'STILL ALIVE' if alive else 'gone'}")
        killed_at = ev.get("done_at") or ev["ts"]
        a = self.s.agents.find_one({"_id": victim}) or {}
        died = a.get("died_at")
        dt = (died - killed_at).total_seconds() if died and a.get("status") == "dead" else None
        limit = config.LEASE_S + 2 * config.HEARTBEAT_S + 2
        self.r.add("d. undertaker marks it dead", dt is not None and dt <= limit,
                   f"{victim} status={a.get('status')} {f'{dt:.1f}s after SIGKILL' if dt is not None else ''} "
                   f"(limit {limit:.0f}s)", dt)
        inc = self.s.incidents.find_one({"_id": inc["_id"]})
        hos = [h for h in inc.get("handoffs") or [] if h.get("from") == victim]
        task = self.s.tasks.find_one({"_id": res.get("task")}) if res.get("task") else None
        if not hos:
            self.r.add("d. nurse takeover from checkpoint", False, f"no handoff from {victim} on {inc['_id']}")
            return
        h = hos[0]
        cp = self.s.checkpoints.find_one({"checkpoint_id": h.get("checkpoint_id")}, {"thread_id": 1})
        ok = (str(h.get("to", "")).startswith("nurse") and cp is not None and task is not None
              and task.get("owner") == h["to"] and int(task.get("generation", 0)) >= 2)
        self.r.add("d. nurse takeover from checkpoint", ok,
                   f"{h.get('to')} claimed {res.get('task')} gen {task.get('generation') if task else '?'}, "
                   f"checkpoint {str(h.get('checkpoint_id'))[:13]}… (thread {cp.get('thread_id') if cp else 'MISSING'}) "
                   f"resume_at={h.get('resume_at')} reconciled={[r.get('result') for r in h.get('reconciled') or []]}",
                   h.get("takeover_s"))
        # verified-before-kill steps were not re-applied; every attempt hit the estate at most once
        reapplied = []
        for st in inc.get("steps") or []:
            if st.get("judged_at") and st["status"] == "verified" and st["judged_at"] < killed_at:
                late = self.s.ledger.count_documents({"step_id": st["step_id"], "ts": {"$gt": killed_at}})
                if late:
                    reapplied.append(st["step_id"])
        killed_step = next((s for s in inc.get("steps") or [] if s.get("task_id") == res.get("task")), None)
        receipts = list(self.w.op_receipts.find({"_id": {"$in": (killed_step or {}).get("attempts") or []}}))
        applied = [r for r in receipts if r.get("effective") or r.get("applied_at")]
        self.r.add("d. no verified step re-applied", not reapplied and len(applied) <= 1,
                   f"steps verified before the kill untouched; {killed_step['op'] if killed_step else '?'} applied "
                   f"{len(applied)}x on the estate ({len(receipts)} receipt(s))" if not reapplied
                   else f"re-sent after kill: {reapplied}")
        spare, secs = self.wait("respawn", lambda: self.s.agents.find_one(
            {"standby_for": victim, "status": {"$ne": "dead"}}), config.NURSE_RESPAWN_S + 15)
        self.r.add("d. supervisor respawns a nurse standby", spare is not None,
                   f"{spare['_id']} on the bench (standby_for {victim})" if spare else
                   f"no standby_for={victim} within {config.NURSE_RESPAWN_S + 15:.0f}s")

    def check_resolved(self, inc: dict, obs: dict, tag: str) -> None:
        inc = self.s.incidents.find_one({"_id": inc["_id"]})
        systems = obs.get("systems_at_close") if self.autoplay and obs.get("systems_at_close") else \
            {d["_id"]: d["status"] for d in self.w.systems.find()}
        if self.red_kill:  # other Red attacks may still be on screen elsewhere: green = this incident's systems
            mine = set(inc.get("affected") or []) | {inc.get("root_cause_system")}
            systems = {k: v for k, v in systems.items() if k in mine}
        m = inc.get("metrics") or {}
        self.r.add(f"{tag} incident resolved, estate green",
                   inc["status"] == "resolved" and all(v == "up" for v in systems.values()),
                   f"{inc['_id']} {inc['status']} ttr={m.get('ttr_s')}s hops={m.get('hops')} attempts={m.get('attempts')} "
                   f"systems={systems}", obs.get("closed_after"))
        pb = self.s.playbooks.find_one({"incident": inc["_id"]})
        ok = bool(pb and len(pb.get("embedding") or []) == config.EMBED_DIM and pb.get("embedding_model")
                  and pb.get("system_version") is not None and pb.get("system") == "mainframe")
        self.r.add(f"{tag} playbook learned", ok,
                   f"{pb['_id']} system={pb['system']} v{pb['system_version']} {pb['embedding_model']} "
                   f"dims={len(pb.get('embedding') or [])} fix={[f['op'] for f in pb.get('fix') or []]}" if pb
                   else "no playbook")

    def night2(self, n1: dict, timeout: float) -> dict | None:
        log("=== NIGHT 2: same fault, the hive remembers ($vectorSearch) ===")
        if not self.autoplay:  # autoplay does the next_round reset itself
            out = self.post("/api/reset", {"mode": "next_round"})
            log(f"POST /api/reset {{mode: next_round}} → closed {out.get('closed_incidents')} incidents")
            self.wait("estate green", lambda: all(d["status"] == "up" for d in self.w.systems.find()), 20)
        obs = self.play_night("bank-night-2", kill=False, timeout=timeout)
        incs = self.night_incidents(obs)
        if len(incs) != 1:
            self.r.add("f. round-2 incident", False, f"{len(incs)} incidents for {obs['run_id']}")
            return None
        inc, prev = incs[0], n1["inc"]
        rec = inc.get("recall") or {}
        hits = rec.get("hits") or []
        top = hits[0] if hits else {}
        self.r.add("f. $vectorSearch recall hit", rec.get("used") is True and top.get("score", 0) >= config.RECALL_THRESHOLD,
                   f"used={rec.get('used')} top={top.get('playbook_id')} score={top.get('score')} "
                   f"filter={rec.get('filter')} model={rec.get('model')}")
        m1, m2 = prev.get("metrics") or {}, inc.get("metrics") or {}
        faster = (m2.get("hops", 9) < m1.get("hops", 0) and m2.get("diagnosis_s", 1e9) < m1.get("diagnosis_s", 0)
                  and inc.get("diagnosis_path") == ["mainframe"])
        self.r.add("f. walk skipped, faster diagnosis", faster,
                   f"path {prev.get('diagnosis_path')} → {inc.get('diagnosis_path')}; hops {m1.get('hops')} → {m2.get('hops')}; "
                   f"diagnosis_s {m1.get('diagnosis_s')} → {m2.get('diagnosis_s')}; ttr {m1.get('ttr_s')} → {m2.get('ttr_s')}")
        self.check_resolved(inc, obs, "f.")
        sc = self.http.get("/api/scorecard").json()
        rounds = {r["round"]: r for r in sc.get("rounds") or []}
        r1, r2 = rounds.get(n1["obs"]["round"]), rounds.get(obs["round"])
        self.r.add("f. scorecard round 1 vs round 2", bool(r1 and r2 and r2.get("recall_used")),
                   f"R1 ttr {r1 and r1.get('avg_ttr_s')}s diag {r1 and r1.get('avg_diagnosis_s')}s hops "
                   f"{r1 and r1.get('avg_hops')} | R2 ttr {r2 and r2.get('avg_ttr_s')}s diag {r2 and r2.get('avg_diagnosis_s')}s "
                   f"hops {r2 and r2.get('avg_hops')} | speedup {sc.get('speedup')}x")
        return {"obs": obs, "inc": inc}

    def gardener(self, timeout: float) -> bool:
        log("=== GARDENER: recurring fault → upgrade request → approve → deploy → prove ===")
        upg, secs = self.wait("upgrade request", lambda: self.s.upgrade_requests.find_one(
            {"system": "mainframe", "fault_class": "batch_abort"}), 30)
        self.r.add("g. sentinel files upgrade request", upg is not None,
                   f"{upg['_id']} '{upg.get('weakness')}' evidence={upg.get('evidence')}" if upg else "none within 30s", secs)
        if not upg:
            return False
        before_md5 = md5(MAINFRAME_PY)
        t0 = time.monotonic()
        approved = None
        while time.monotonic() - t0 < timeout:
            doc = self.s.upgrade_requests.find_one({"_id": upg["_id"]})
            if doc["status"] in ("proven", "rejected"):
                break
            if doc["status"] == "deployed" and (doc.get("prove") or {}).get("incident_opened"):
                break
            if self.autoplay:  # the war room auto-approves; just record that it did
                approved = approved or self.s.approvals.find_one(
                    {"kind": "upgrade", "upgrade": upg["_id"], "status": "approved", "decided_by": "autoplay"})
                time.sleep(0.5)
                continue
            apr = self.s.approvals.find_one({"kind": "upgrade", "upgrade": upg["_id"], "status": "pending"})
            if apr and not approved:
                time.sleep(self.approve_delay)
                approved = apr
                self.approve(apr["_id"])
            time.sleep(0.5)
        doc = self.s.upgrade_requests.find_one({"_id": upg["_id"]})
        tests = doc.get("tests") or {}
        self.r.add("g. gardener plans + tests + asks approval", bool(approved and tests.get("passed") and doc.get("diff")),
                   f"diff {doc.get('diff_lines')} lines, tests passed={tests.get('passed')} "
                   f"(pytest={tests.get('pytest_passed')}, replay={tests.get('replay_passed')}), approval "
                   f"{approved['_id'] if approved else 'NEVER REQUESTED'}"
                   + (f"; reason: {doc.get('reason')}" if doc.get("reason") else ""))
        mf = self.w.systems.find_one({"_id": "mainframe"}) or {}
        st = self.mf_state()
        stale = list(self.s.playbooks.find({"system": "mainframe", "system_version": {"$lt": mf.get("version", 0)}},
                                           {"stale": 1}))
        # Under autoplay + Red fleet the upgrade can deploy before this phase starts, so before_md5 may already be the
        # patched file: compare against the Gardener's backup of the original when it has one.
        backup = Path(doc.get("backup") or "")
        if backup.is_file():
            before_md5 = md5(backup)
        deployed = (md5(MAINFRAME_PY) != before_md5 and mf.get("version") == 2 and st.get("validation_enabled") is True
                    and doc.get("version_after") == 2)
        self.r.add("g. deployed: mainframe.py patched, v2", deployed,
                   f"mainframe version {doc.get('version_before')}→{mf.get('version')}, validation_enabled="
                   f"{st.get('validation_enabled')}, backup {Path(doc.get('backup') or '?').name}")
        stale_ok = bool(stale) and all(p.get("stale") for p in stale)
        if not stale_ok:  # the sentinel notices the version bump on its next sweep
            time.sleep(3)
            stale = list(self.s.playbooks.find({"system": "mainframe", "system_version": {"$lt": mf.get("version", 0)}},
                                               {"stale": 1}))
            stale_ok = bool(stale) and all(p.get("stale") for p in stale)
        self.r.add("g. old playbooks marked stale", stale_ok,
                   f"{sum(1 for p in stale if p.get('stale'))}/{len(stale)} mainframe v1 playbooks stale")
        prove = doc.get("prove") or {}
        self.r.add("g. upgrade proven (replayed fault, no incident)", doc["status"] == "proven",
                   f"status={doc['status']} prove fired {prove.get('fault')} via {prove.get('via')}, window "
                   f"{prove.get('window_s')}s, incident_opened={prove.get('incident_opened')}", time.monotonic() - t0)
        return doc["status"] == "proven"

    def night3(self, watch_s: float) -> None:
        log("=== NIGHT 3: the same attack after the upgrade ===")
        out = self.start("bank-night-3")
        run_id, t0 = out["run_id"], time.monotonic()
        max_active, worst = 0, {}
        # With the Red fleet on, other attacks may open their own incidents on unix/portal this night; the beat is
        # that the replayed batch_abort opens none: count only incidents that touch the mainframe or its fault.
        mine = {"$or": [{"fault_class": "batch_abort"}, {"system": "mainframe"}, {"affected": "mainframe"},
                        {"root_cause_system": "mainframe", "kind": {"$ne": "security"}}]} if self.red_kill else {}
        while time.monotonic() - t0 < watch_s:
            max_active = max(max_active, self.s.incidents.count_documents({"active": True, **mine}))
            for d in self.w.systems.find():
                if d["status"] != "up" and (d["_id"] == "mainframe" or not self.red_kill):
                    worst[d["_id"]] = d["status"]
            time.sleep(0.5)
        ev = self.w.chaos_events.find_one({"run_id": run_id, "fault": "batch_abort",
                                           "source": {"$ne": "red-fleet"}}) or {}
        st = self.mf_state()
        n = self.s.incidents.count_documents({"run_id": run_id, **mine})
        ok = (ev.get("status") == "ok" and n == 0 and max_active == 0 and not worst
              and st["batch"]["status"] == "OK")
        self.r.add("g. night 3: batch_abort shrugged off", ok,
                   f"{run_id}: batch_abort {ev.get('status')}, {n} incidents in {watch_s:.0f}s, batch {st['batch']['status']}, "
                   f"rejected={st['batch'].get('rejected')}, unhealthy={worst or 'none'}", time.monotonic() - t0)

    def check_ai(self, inc_ids: list[str]) -> None:
        """Optional (--check-ai; needs AI_SECOND_OPINION=1 and ANTHROPIC_API_KEY on the stack): Claude's second opinion
        landed on each night's incident and every proposed op carries a named verdict. Advisory, not one of the 26."""
        parts, ok = [], bool(inc_ids)
        for inc_id in inc_ids:
            ai = (self.s.incidents.find_one({"_id": inc_id}, {"ai": 1}) or {}).get("ai") or {}
            ops, vs = ai.get("proposed_ops") or [], ai.get("verdicts") or []
            judged = (ai.get("status") == "done" and ai.get("kind") == "second_opinion" and bool(ops)
                      and len(vs) == len(ops) and ai.get("latency_ms") is not None
                      and all(v.get("verdict") in ("accepted", "overruled") and v.get("rule") and v.get("reason") for v in vs))
            if config.AI_DRIVES and inc_id == inc_ids[0]:  # night 1 walks the chain, so the AI decides its plan
                steps = (self.s.incidents.find_one({"_id": inc_id}, {"steps": 1}) or {}).get("steps") or []
                src = {s.get("source") for s in steps}
                good = (ai.get("decision") in ("ai_plan", "rules_plan") and bool(ai.get("decision_reason"))
                        and src == ({"ai"} if ai.get("decision") == "ai_plan" else {"policy"})
                        and (judged or ai.get("status") == "offline"))
                parts.append(f"{inc_id} decision={ai.get('decision')} ({str(ai.get('decision_reason'))[:60]})")
            elif not ai and inc_id != inc_ids[0]:
                good = True  # recall night: a fresh playbook short-circuits, Claude isn't asked
                parts.append(f"{inc_id} recall hit: AI skipped")
            else:
                good = judged
            ok = ok and good
            parts.append(f"{inc_id} {ai.get('status', 'absent')} {ai.get('latency_ms')}ms conf={ai.get('confidence')} "
                         f"cites={len(ai.get('cites') or [])} {sum(v.get('verdict') == 'accepted' for v in vs)} accepted/"
                         f"{sum(v.get('verdict') == 'overruled' for v in vs)} overruled/{len(ai.get('missed_ops') or [])} missed")
        self.r.add("h. AI second opinion judged (opt-in)", ok, "; ".join(parts) or "no incidents")

    def final_reset(self) -> None:
        log("POST /api/reset {mode: full} (demo reset: Gardener revert)")
        out = self.post("/api/reset", {"mode": "full"})
        time.sleep(2)
        st = self.mf_state()
        mf = self.w.systems.find_one({"_id": "mainframe"}) or {}
        gr = out.get("gardener_revert") or {}
        ok = st.get("validation_enabled") is False and mf.get("version") == 1 and not self.s.playbooks.count_documents({})
        self.r.add("g. demo reset restores legacy mainframe.py", ok,
                   f"revert changed={gr.get('changed') if isinstance(gr, dict) else gr}, validation_enabled="
                   f"{st.get('validation_enabled')}, mainframe v{mf.get('version')}, md5 {md5(MAINFRAME_PY)[:8]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("WARROOM_URL", f"http://127.0.0.1:{config.WARROOM_PORT}"))
    ap.add_argument("--no-reset", action="store_true", help="don't full-reset before night 1")
    ap.add_argument("--keep", action="store_true", help="don't full-reset (Gardener revert) at the end")
    ap.add_argument("--approve-delay", type=float, default=1.5, help="seconds a pending approval stays on screen")
    ap.add_argument("--night-timeout", type=float, default=240)
    ap.add_argument("--night3-watch", type=float, default=env_float("NIGHT3_WATCH_S", 25),
                    help="seconds to watch night 3 for incidents (env NIGHT3_WATCH_S)")
    ap.add_argument("--red-kill", action="store_true",
                    help="stack runs the Red Assassin (stage mode): wait for its night-1 kill instead of POST /api/kill")
    ap.add_argument("--autoplay", action="store_true", default=config.DEMO_AUTOPLAY,
                    help="the stack runs with DEMO_AUTOPLAY=1: don't click Night 2/3, next round or later approvals")
    ap.add_argument("--check-ai", action="store_true",
                    help="also check Claude's second opinion on each night's incident (skipped without AI_SECOND_OPINION=1 + key)")
    a = ap.parse_args()
    rep = Report()
    d = Demo(a.base, a.approve_delay, rep, autoplay=a.autoplay, red_kill=a.red_kill)
    log(f"demo_check against {a.base} (systems {config.SYSTEMS_URL}, db {config.WORLD_DB}/{config.SWARM_DB})"
        + (" [autoplay]" if a.autoplay else ""))
    try:
        if d.preflight(reset=not a.no_reset):
            n1 = n2 = None
            n1 = d.night1(a.night_timeout)
            if n1:
                n2 = d.night2(n1, a.night_timeout)
                if n2 and d.gardener(a.night_timeout):
                    d.night3(a.night3_watch)
            if a.check_ai and config.ANTHROPIC_API_KEY and (config.AI_SECOND_OPINION or config.AI_DRIVES):
                d.check_ai([n["inc"]["_id"] for n in (n1, n2) if n])
            elif a.check_ai:
                log("--check-ai skipped: needs AI_SECOND_OPINION=1 or AI_DRIVES=1, and ANTHROPIC_API_KEY")
            if not a.keep:
                d.final_reset()
    except KeyboardInterrupt:
        rep.add("aborted", False, "interrupted")
    except Exception as e:  # report, don't hide
        import traceback
        traceback.print_exc()
        rep.add("crashed", False, f"{type(e).__name__}: {e}")
    return 0 if rep.table() else 1


if __name__ == "__main__":
    sys.exit(main())
