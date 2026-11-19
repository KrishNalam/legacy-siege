"""Record the war room headlessly while driving the full demo, and write a timestamped beat log.

    .venv/bin/python -m scripts.record_demo                       # → ~/projects/legacy-siege-media/raw-<ts>.webm + beats-<ts>.json
    .venv/bin/python -m scripts.record_demo --base http://127.0.0.1:8770 --out /some/dir
    .venv/bin/python -m scripts.record_demo --keep                # leave the upgraded estate on screen afterwards
    .venv/bin/python -m scripts.record_demo --drift               # + one schema-drift round before the Atlas view

The stack must already be running (`run.py`), and MONGODB_URI / WORLD_DB / SWARM_DB must point at the same
databases as that stack (the beat monitor reads MongoDB directly, exactly like scripts.demo_check).

What it does: opens the war room in Playwright Chromium at 1920x1080 with video recording on, then runs the
demo_check flow unchanged (Reset Full → Night 1 → Approve → Kill a worker → green → Reset Next round →
Night 2 → Approve ×2 → Night 3), opens the Atlas view, and closes the browser. demo_check's assertions run
as usual, so the beat log also says whether the take is a valid demo. A monitor thread polls MongoDB
every 0.25 s and timestamps each moment (cascade, diagnosis, ACK ≠ APPLIED, SIGKILL, handoff, recall,
upgrade deployed / proven …). All times in the beat log are seconds into the raw video.

Beats for the 10-beat script (docs/video-script.md), stamped whenever the stack produces them:
  ai_checked / ai_overruled  incident.ai (kind second_opinion) decided: Claude's plan runs / an op overruled (AI_DRIVES)
  schema_drift / drift_reused  sentinel "feed schema changed" → schema_mappings history entry (new / reused);
                             `--drift` plays bank-drift after night 3 so the take has one
  red_freeze                 a chaos_events agent_freeze fired by a red-* striker → the next Nurse handoff (RED_FLEET=1)
Missing ones are cut from a fallback window (scripts.cut_video FALLBACK) or pointed at by hand with --at.

Then cut it: `.venv/bin/python -m scripts.cut_video ~/projects/legacy-siege-media/beats-<ts>.json`.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # also runnable as a plain script
from common import config  # noqa: E402
from scripts.beatlog import script_beats  # noqa: E402
from scripts.demo_check import Demo, Report, log  # noqa: E402

MEDIA = Path.home() / "projects" / "legacy-siege-media"
VIEW = {"width": 1920, "height": 1080}


def find_chromium() -> str | None:
    """Newest Playwright Chromium in the cache (the janitor prunes old builds), else Playwright's default."""
    hits = sorted(glob.glob(str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux64/chrome")),
                  key=lambda p: int(p.split("chromium-")[1].split("/")[0]))
    return hits[-1] if hits else None


# ---------------------------------------------------------------------------------------------- beat log
class Clock:
    """Wall clock relative to the moment the recorded page was created (≈ first video frame)."""

    def __init__(self):
        self.t0 = time.monotonic()
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.seen: set[str] = set()

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def event(self, name: str, once: str | None = None, **info) -> None:
        with self.lock:
            if once is not None:
                if once in self.seen:
                    return
                self.seen.add(once)
            self.events.append({"name": name, "t": self.now(), **info})
        log(f"[beat] {name} {info if info else ''}".rstrip())


class Monitor(threading.Thread):
    """Polls MongoDB and stamps the first time each demo moment becomes true."""

    def __init__(self, demo: "RecDemo", clock: Clock):
        super().__init__(daemon=True)
        self.d, self.c = demo, clock
        self.stop = threading.Event()
        self.run_id: str | None = None
        self.since = datetime.now(timezone.utc)  # ignore thoughts / mappings older than the take

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.poll()
            except Exception as e:  # never let the monitor kill the take
                log(f"[monitor] {type(e).__name__}: {e}")
            self.stop.wait(0.25)

    def poll(self) -> None:
        s, w, c, run = self.d.s, self.d.w, self.c, self.run_id
        if run:
            systems = {d["_id"]: d for d in w.systems.find({}, {"status": 1, "blames": 1, "metrics.batch_status": 1})}
            ux, po = systems.get("unix", {}), systems.get("portal", {})
            if any(d.get("status") != "up" for d in systems.values()):
                c.event("estate_hit", once=f"hit:{run}", run=run)
            if po.get("status") == "degraded" and ux.get("status") == "degraded" and po.get("blames") == "unix" \
                    and ux.get("blames") == "mainframe":
                c.event("cascade", once=f"cascade:{run}", run=run)
            inc = s.incidents.find_one({"run_id": run}, sort=[("opened_at", 1)])
            if inc:
                iid = inc["_id"]
                c.event("incident_open", once=f"open:{run}", run=run, incident=iid)
                if (inc.get("recall") or {}).get("used"):
                    top = (inc["recall"].get("hits") or [{}])[0]
                    c.event("recall_hit", once=f"recall:{run}", run=run, score=top.get("score"))
                if inc.get("diagnosis_path") and inc.get("root_cause_system"):
                    c.event("diagnosed", once=f"diag:{run}", run=run, path=inc["diagnosis_path"],
                            diagnosis_s=(inc.get("metrics") or {}).get("diagnosis_s"))
                if s.thoughts.find_one({"incident": iid, "kind": "reject", "agent": "guard-1"}):
                    c.event("lie_caught", once=f"lie:{run}", run=run)
                for st in inc.get("steps") or []:
                    if st.get("status") == "verified":
                        c.event("step_verified", once=f"ver:{st['step_id']}", run=run, op=st.get("op"))
                if inc.get("handoffs"):
                    h = inc["handoffs"][0]
                    c.event("handoff", once=f"handoff:{run}", run=run, to=h.get("to"), takeover_s=h.get("takeover_s"))
                if inc["status"] in ("resolved", "failed") and not inc.get("active"):
                    m = inc.get("metrics") or {}
                    c.event("resolved", once=f"resolved:{run}", run=run, status=inc["status"], ttr_s=m.get("ttr_s"),
                            hops=m.get("hops"), diagnosis_s=m.get("diagnosis_s"))
            ev = w.chaos_events.find_one({"run_id": run, "fault": "agent_kill", "status": "ok"})
            if ev and (ev.get("result") or {}).get("ok"):
                c.event("sigkill", once=f"kill:{run}", run=run, agent=ev["result"].get("agent"))
        for apr in s.approvals.find({"status": {"$in": ["pending", "approved", "denied"]}}):
            c.event("approval_pending", once=f"apr:{apr['_id']}", id=apr["_id"], kind=apr.get("kind"),
                    desc=(apr.get("desc") or "")[:60])
            if apr["status"] != "pending":
                c.event("approval_decided", once=f"dec:{apr['_id']}", id=apr["_id"], kind=apr.get("kind"))
        for u in s.upgrade_requests.find({}, {"status": 1}):
            c.event(f"upgrade_{u['status']}", once=f"upg:{u['_id']}:{u['status']}", id=u["_id"])
        self.poll_script_beats()

    def poll_script_beats(self) -> None:
        """Run-independent moments for the 10-beat script: Claude's checked plan, schema drift, Red FREEZE."""
        s, w, c = self.d.s, self.d.w, self.c
        for inc in s.incidents.find({"ai.kind": "second_opinion", "ai.status": {"$in": ["thinking", "done"]}},
                                    {"run_id": 1, "ai.status": 1, "ai.decision": 1, "ai.decision_reason": 1,
                                     "ai.verdicts": 1}):
            ai, iid = inc["ai"], inc["_id"]
            c.event("ai_thinking", once=f"ai:{iid}", incident=iid, run=inc.get("run_id"))
            if ai.get("status") == "done" and ai.get("decision"):
                bad = [v for v in ai.get("verdicts") or [] if v.get("verdict") != "accepted"]
                c.event("ai_decided", once=f"aid:{iid}", incident=iid, run=inc.get("run_id"),
                        decision=ai["decision"], reason=(ai.get("decision_reason") or "")[:120])
                if bad:
                    c.event("ai_overruled", once=f"aio:{iid}", incident=iid, op=bad[0].get("op"),
                            target=bad[0].get("target"), rule=bad[0].get("rule"))
        for th in s.thoughts.find({"agent": {"$regex": "^sentinel"}, "text": {"$regex": "feed schema changed"},
                                   "ts": {"$gt": self.since}}, {"text": 1}):
            c.event("drift_noticed", once=f"drift:{th['_id']}", text=th["text"][:120])
        for m in s.schema_mappings.find({}, {"history": 1}):
            for h in m.get("history") or []:
                ts = h.get("ts")
                if ts and (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)) > self.since:
                    c.event("mapping_reused" if h.get("reused") else "mapping_learned",
                            once=f"map:{m['_id']}:{h.get('version')}:{ts}", frm=h.get("from"), to=h.get("to"),
                            version=h.get("version"), incident=h.get("incident"))
        for ev in w.chaos_events.find({"fault": "agent_freeze"}, {"status": 1, "result": 1, "by": 1, "who": 1}):
            by = ev.get("by") or ev.get("who")
            c.event("red_freeze", once=f"frz:{ev['_id']}", id=ev["_id"], by=by)
            if ev.get("status") in ("ok", "failed"):
                c.event("red_thaw", once=f"thaw:{ev['_id']}", id=ev["_id"], by=by, ok=ev["status"] == "ok",
                        agent=(ev.get("result") or {}).get("agent"))
        for th in s.thoughts.find({"agent": {"$regex": "^red-"}, "text": {"$regex": " REFLECT "},
                                   "ts": {"$gt": self.since}}, {"agent": 1}):
            c.event("red_reflect", once=f"rr:{th['_id']}", agent=th["agent"])
        for inc in s.incidents.find({"handoffs.0": {"$exists": True}}, {"handoffs": 1}):
            for i, h in enumerate(inc["handoffs"]):
                c.event("takeover", once=f"to:{inc['_id']}:{i}", incident=inc["_id"], frm=h.get("from"),
                        to=h.get("to"), takeover_s=h.get("takeover_s"))


class RecDemo(Demo):
    """demo_check's driver, with every API call and night stamped on the clock."""

    def __init__(self, *a, clock: Clock, **kw):
        super().__init__(*a, **kw)
        self.clock = clock
        self.monitor: Monitor | None = None

    def post(self, path: str, body: dict | None = None) -> dict:
        out = super().post(path, body)
        name = path.strip("/").replace("api/", "").replace("/", ":")
        if not name.startswith("approvals"):
            self.clock.event(f"api_{name}", body=body or {}, run=out.get("run_id"))
        if out.get("run_id") and self.monitor:
            self.monitor.run_id = out["run_id"]
        return out

    def play_night(self, scenario: str, kill: bool, timeout: float) -> dict:
        self.clock.event("night_start", scenario=scenario)
        obs = super().play_night(scenario, kill, timeout)
        self.clock.event("night_end", scenario=scenario, run=obs["run_id"])
        return obs

    def night3(self, watch_s: float) -> None:
        self.clock.event("night_start", scenario="bank-night-3")
        super().night3(watch_s)
        self.clock.event("night_end", scenario="bank-night-3")

    def drift(self, timeout: float) -> bool:
        """One schema-drift round (bank-drift: unix renames balance → acct_bal) for the drift beat. Not one of
        demo_check's scored beats, so it only logs; scripts.drift_check is the real check."""
        self.clock.event("night_start", scenario="bank-drift")
        self.wait("good sample", lambda: self.s.schema_samples.find_one({"_id": "good:unix:xfer_manifest"}), 30)
        started = datetime.now(timezone.utc)
        self.post("/api/scenario/bank-drift")
        inc, secs = self.wait("drift resolved", lambda: self.s.incidents.find_one(
            {"fault_class": "schema_drift", "active": False, "opened_at": {"$gt": started}}), timeout)
        log(f"drift round: {inc and inc['_id']} {inc and inc.get('status')} after {secs:.0f}s")
        self.clock.event("night_end", scenario="bank-drift")
        return bool(inc)


# ---------------------------------------------------------------------------------------------- beats
def derive_beats(events: list[dict], offset: float) -> list[dict]:
    """Turn raw events into named [t_start, t_end] beats (seconds into the raw video)."""
    def first(name, **match):
        for e in events:
            if e["name"] == name and all(e.get(k) == v for k, v in match.items()):
                return e
        return None

    def t(e, d=0.0):
        return None if e is None else round(max(0.0, e["t"] - offset + d), 2)

    runs = [e.get("run") for e in events if e["name"] == "api_scenario:bank-night-1"] + \
           [e.get("run") for e in events if e["name"] == "api_scenario:bank-night-2"] + \
           [e.get("run") for e in events if e["name"] == "api_scenario:bank-night-3"]
    r1, r2, r3 = (runs + [None] * 3)[:3]
    n1 = first("night_start", scenario="bank-night-1")
    n2 = first("night_start", scenario="bank-night-2")
    n3 = first("night_start", scenario="bank-night-3")
    n3_end = first("night_end", scenario="bank-night-3")
    restart_apr = next((e for e in events if e["name"] == "approval_pending" and e.get("kind") == "step"
                        and "restart_batch" in (e.get("desc") or "")), None)
    upg_apr = next((e for e in events if e["name"] == "approval_pending" and e.get("kind") == "upgrade"), None)
    beats = []

    def add(name, a, b, **info):
        if a is not None and b is not None and b > a:
            beats.append({"name": name, "t_start": a, "t_end": b, **info})

    add("idle", t(first("api_reset", body={"mode": "full"}), 2), t(n1))
    add("night1_attack", t(n1), t(first("cascade", run=r1), 2) or t(first("incident_open", run=r1), 2))
    add("cascade_trace", t(first("incident_open", run=r1)), t(first("diagnosed", run=r1), 2.5))
    add("lie_caught", t(first("lie_caught", run=r1), -2.5), t(first("lie_caught", run=r1), 6))
    add("approval", t(restart_apr, -0.5), t(first("approval_decided", id=(restart_apr or {}).get("id")), 1))
    add("kill_takeover", t(first("sigkill", run=r1), -1), t(first("handoff", run=r1), 6))
    add("night1_resolved", t(first("resolved", run=r1), -1.5), t(first("resolved", run=r1), 4))
    add("night2_recall", t(n2), t(first("diagnosed", run=r2), 4))
    add("night2_resolved", t(first("resolved", run=r2), -1.5), t(first("resolved", run=r2), 4))
    add("gardener", t(upg_apr, -2) or t(first("upgrade_awaiting_approval"), -2),
        t(first("upgrade_proven"), 2) or t(first("upgrade_deployed"), 6))
    add("night3", t(n3), t(n3_end))
    add("atlas", t(first("atlas_open")), t(first("atlas_close")))
    beats += script_beats(events, t)
    beats.sort(key=lambda b: b["t_start"])
    return beats


def ffprobe_duration(p: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("WARROOM_URL", f"http://127.0.0.1:{config.WARROOM_PORT}"))
    ap.add_argument("--out", type=Path, default=MEDIA)
    ap.add_argument("--chrome", default=os.environ.get("RECORD_CHROME") or find_chromium())
    ap.add_argument("--approve-delay", type=float, default=3.0, help="seconds the Beekeeper modal stays on screen")
    ap.add_argument("--night-timeout", type=float, default=240)
    ap.add_argument("--night3-watch", type=float, default=20)
    ap.add_argument("--atlas-s", type=float, default=9, help="seconds on the Atlas view")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--keep", action="store_true", help="don't full-reset (Gardener revert) after recording")
    ap.add_argument("--drift", action="store_true",
                    help="after night 3, play bank-drift once so the take has the schema-drift beat")
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    a.out.mkdir(parents=True, exist_ok=True)
    tmp = a.out / f".rec-{ts}"
    raw, beats_path = a.out / f"raw-{ts}.webm", a.out / f"beats-{ts}.json"
    rep = Report()
    console: list[str] = []
    log(f"record_demo against {a.base} (systems {config.SYSTEMS_URL}, db {config.WORLD_DB}/{config.SWARM_DB}) "
        f"chrome={a.chrome}")
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path=a.chrome, headless=True,
                               args=["--no-sandbox", "--force-color-profile=srgb", "--hide-scrollbars"])
        ctx = br.new_context(viewport=VIEW, device_scale_factor=1, record_video_dir=str(tmp), record_video_size=VIEW)
        page = ctx.new_page()
        clock = Clock()
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
        d = RecDemo(a.base, a.approve_delay, rep, clock=clock)
        mon = Monitor(d, clock)
        d.monitor = mon
        ok = False
        try:
            page.goto(a.base + "/diagram", wait_until="domcontentloaded")
            page.wait_for_selector("#ov-nodes > *", timeout=30000)
            clock.event("page_ready")
            mon.start()
            if d.preflight(reset=not a.no_reset):
                n1 = d.night1(a.night_timeout)
                if n1:
                    n2 = d.night2(n1, a.night_timeout)
                    if n2 and d.gardener(a.night_timeout):
                        d.night3(a.night3_watch)
                        ok = True
                        if a.drift:
                            d.drift(a.night_timeout)
            page.wait_for_timeout(1500)
            clock.event("atlas_open")
            page.click("#btn-more")  # RESET + menu → DETAILS (MongoDB collections, memory search, numbers)
            page.click("#btn-details")
            page.wait_for_selector("#det-body > *", timeout=20000)
            page.wait_for_timeout(int(a.atlas_s * 1000))
            clock.event("atlas_close")
            page.wait_for_timeout(500)
        except KeyboardInterrupt:
            rep.add("aborted", False, "interrupted")
        except Exception as e:
            import traceback
            traceback.print_exc()
            rep.add("crashed", False, f"{type(e).__name__}: {e}")
        finally:
            mon.stop.set()
            wall = clock.now()
            video = page.video
            ctx.close()  # flushes the video
            br.close()
            src = Path(video.path()) if video else None
            if src and src.exists():
                shutil.move(str(src), raw)
            shutil.rmtree(tmp, ignore_errors=True)
    if not a.keep:
        d.final_reset()
    all_pass = rep.table()
    dur = ffprobe_duration(raw) if raw.exists() else None
    # Video starts a beat after new_page(); assume the whole wall/video mismatch is start latency.
    offset = round(min(max((wall - dur) if dur else 0.0, 0.0), 3.0), 2)
    inc = {}
    for n, sc in ((1, "bank-night-1"), (2, "bank-night-2")):
        e = next((e for e in clock.events if e["name"] == "resolved" and e.get("run") and
                  any(x.get("run") == e["run"] and x["name"] == f"api_scenario:{sc}" for x in clock.events)), None)
        if e:
            inc[f"night{n}"] = {k: e.get(k) for k in ("ttr_s", "hops", "diagnosis_s")}
    out = {
        "video": raw.name, "video_duration_s": dur, "wall_s": wall, "video_offset_s": offset,
        "recorded_at": datetime.now(tz=timezone.utc).isoformat(), "base": a.base,
        "db": f"{config.WORLD_DB}/{config.SWARM_DB}", "pace": os.environ.get("PACE"),
        "demo_ok": bool(ok and all_pass), "metrics": inc,
        "beats": derive_beats(clock.events, offset),
        "events": clock.events,
        "checks": [{"beat": b, "ok": k, "secs": s, "detail": det} for b, k, s, det in rep.rows],
        "console_errors": console[:50],
    }
    beats_path.write_text(json.dumps(out, indent=1, default=str))
    log(f"video  {raw} ({dur and round(dur, 1)} s)")
    log(f"beats  {beats_path}  demo_ok={out['demo_ok']}")
    for b in out["beats"]:
        log(f"  {b['name']:<16} {b['t_start']:7.1f} → {b['t_end']:7.1f}")
    return 0 if out["demo_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
