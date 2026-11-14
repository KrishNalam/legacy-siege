"""Red Commander + 2 Scouts (opt-in ``RED_COMMANDER=1``, only meaningful with ``RED_FLEET=1``).

The Strikers each decide alone; the Commander plans *campaigns*: an ordered combo of vetted Runner faults
(e.g. a lying mainframe, then FREEZE the worker that sends the next step), assigns each step to a Striker on
the shared Red board (``world.red_agents.<striker>.order``), waits for it to land and reflects on the outcome
(did the Guard catch it, did a Nurse take over, how fast was recovery).

* Scouts (``red-scout-hive``, ``red-scout-systems``): cheap observers, no model. Each tick they read the Hive /
  the estate and post findings on their own board entry; the Commander plans from those findings.
* Commander (``red-commander``): LangGraph OBSERVE -> THINK -> ACT -> REFLECT. Claude picks ONE campaign from the
  vetted combos that are feasible right now (labelled ``claude``); anything else, no key, a timeout or bad JSON ->
  deterministic policy (labelled ``policy (no reasoning)``).
* The Commander never fires anything itself. A Striker executes its order through its own persona menu +
  ``validate`` (params from the menu, never the model) and ``runner.fire``; the chaos event carries
  ``red.campaign = {id, step, of, combo}`` and later the campaign's reflection.

Runs as a thread inside the fleet supervisor (no extra process); ``--agent red-commander`` runs it alone.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TypedDict

from common import config, llm
from common.db import now, world
from common.events import think

from . import core, learning
from .personas import ARMED, KEYS, PERSONAS, SPOILER, VULN_OF, validate

log = logging.getLogger("chaos.redfleet.commander")

COMMANDER_ID = "red-commander"
SCOUT_HIVE, SCOUT_SYSTEMS = "red-scout-hive", "red-scout-systems"
AGENT_FAULTS = ("agent_kill", "agent_freeze")


def enabled() -> bool:
    return os.environ.get("RED_COMMANDER", "").strip().lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    return max(0.0, config.env_float(name, default))


def _roster() -> set[str]:
    raw = os.environ.get("RED_FLEET_AGENTS", "").strip()
    return {i.strip() for i in raw.split(",") if i.strip() in PERSONAS} if raw else set(PERSONAS)


# ------------------------------------------------------------------ Scouts: cheap observers, no model
def scout_hive(snap: dict) -> dict:
    agents = snap.get("agents") or []
    pending = []
    for inc in snap.get("incidents") or []:
        for st in inc.get("pending_steps") or []:
            if st.get("system") and st["system"] not in pending:
                pending.append(st["system"])
    return {
        "busy_workers": [a["id"] for a in agents if a["role"] == "worker" and a["status"] == "working"],
        "mid_send": [a["id"] for a in snap.get("mid_send") or []],
        "idle_workers": [a["id"] for a in agents if a["role"] == "worker" and a["status"] != "working"],
        "nurses_free": [a["id"] for a in agents if a["role"] == "nurse" and a["status"] != "working"],
        "queens": len([a for a in agents if a["role"] == "queen"]),
        "open_incidents": [i["id"] for i in snap.get("incidents") or []],
        "pending_step_systems": pending,
    }


def scout_systems(snap: dict) -> dict:
    systems = snap.get("systems") or {}
    weak = {s: d.get("faults_active") for s, d in systems.items() if d.get("faults_active")}
    return {
        "weak": weak,
        "degraded": [s for s, d in systems.items() if d.get("status") not in (None, "healthy", "ok", "up")],
        "open_vulns": {s: d.get("open_vulns") for s, d in systems.items() if d.get("open_vulns")},
        "lying": [s for s, f in weak.items() if "lying_success" in (f or []) or "slow_apply" in (f or [])],
        "healthy": [s for s in systems if s not in weak],
    }


def _summary_hive(f: dict) -> str:
    return (f"{len(f['busy_workers'])} workers busy ({len(f['mid_send'])} mid-send), {len(f['nurses_free'])} nurses "
            f"free, {len(f['open_incidents'])} open incidents; next steps land on "
            f"{', '.join(f['pending_step_systems']) or 'nothing'}")


def _summary_systems(f: dict) -> str:
    weak = "; ".join(f"{s}: {','.join(v)}" for s, v in f["weak"].items()) or "none"
    return f"weak systems: {weak}. Open vulns on {', '.join(f['open_vulns']) or 'none'}"


SCOUT_REPORT_S = 15.0
SCOUTS = {SCOUT_HIVE: ("Hive Scout", scout_hive, _summary_hive),
          SCOUT_SYSTEMS: ("Systems Scout", scout_systems, _summary_systems)}


def post_findings(snap: dict) -> dict:
    """Both Scouts observe and post on the Red board; a thought only when the picture changes."""
    out = {}
    for sid, (name, fn, summarize) in SCOUTS.items():
        findings = fn(snap)
        summary = summarize(findings)
        prev = world().red_agents.find_one({"_id": sid}, {"reported": 1, "reported_at": 1}) or {}
        fields = {"name": name, "kind": "scout", "status": "watching", "findings": findings, "summary": summary,
                  "run_id": snap.get("run_id"), "pid": os.getpid(), "last_seen": now()}
        quiet = prev.get("reported_at") and (now() - prev["reported_at"]).total_seconds() < SCOUT_REPORT_S
        if snap.get("night_running") and prev.get("reported") != summary and not quiet:
            think(sid, core.ROLE, f"{name}: {summary}.")   # the feed hears a changed picture at most every 15 s
            fields.update(reported=summary, reported_at=now())
        world().red_agents.update_one({"_id": sid}, {"$set": fields}, upsert=True)
        out[sid] = findings
    return out


def read_findings() -> dict:
    return {d["_id"]: d.get("findings") or {} for d in world().red_agents.find({"_id": {"$in": list(SCOUTS)}})}


# ------------------------------------------------------------------ vetted combos
COMBOS = {
    "lie-then-freeze": ("ACK lie on {s}, then FREEZE the worker that sends the next step",
                        "Q1+Q2: Guard must catch ACK != APPLIED while a stalled lease is taken over"),
    "lie-then-kill": ("ACK lie on {s}, then SIGKILL the worker that sends the next step",
                      "Q1+Q2: Guard read-back and Nurse resume from checkpoint at once"),
    "false-log-then-kill": ("false log on {root} blaming {wrong}, then SIGKILL the next sender",
                            "Do they trust read-back over logs while a worker dies mid-step?"),
    "break-then-drift": ("break {s} mid-repair, then rename the unix balance field",
                         "Compound faults: re-plan plus adapt to a changed layout"),
    "kill-then-exploit": ("SIGKILL a busy worker, then walk in through {vuln}",
                          "An intruder during a takeover: detect, patch, retest"),
}


def _worker_item(menu: list[dict], fault: str) -> dict | None:
    """Best worker victim for an agent fault: someone mid-send, else the armed next-sender."""
    items = [m for m in menu if m["fault"] == fault and m.get("role") == "worker"]
    return (next((m for m in items if m.get("phase") == "step_sent" and m.get("victim") != ARMED), None)
            or next((m for m in items if m.get("victim") == ARMED), None) or (items[0] if items else None))


def _key(item: dict) -> dict:
    return {k: item[k] for k in KEYS if k in item and item[k] is not None}


def combos(snap: dict, cfg: core.Settings, findings: dict | None = None) -> list[dict]:
    """Every vetted combo whose steps are on the right Strikers' menus right now (and whose Strikers run)."""
    findings = findings or {}
    hive = findings.get(SCOUT_HIVE) or scout_hive(snap)
    estate = findings.get(SCOUT_SYSTEMS) or scout_systems(snap)
    roster = _roster()
    menus = {pid: PERSONAS[pid].menu(snap, cfg) for pid in roster}
    pending = hive.get("pending_step_systems") or []
    s = pending[0] if pending else "mainframe"
    inc = (snap.get("incidents") or [{}])[0]
    root = inc.get("root_cause_system") or inc.get("system") or "mainframe"
    wrong = next((b for b in reversed(core.config.SYSTEM_IDS) if b != root), "unix")

    def item(pid: str, **want):
        if pid not in menus:
            return None
        if want.get("fault") in AGENT_FAULTS:
            return _worker_item(menus[pid], want["fault"])
        return next((m for m in menus[pid] if all(m.get(k) == v for k, v in want.items())), None)

    out = []

    def add(cid: str, steps: list[tuple[str, dict | None]], **fmt):
        if all(i is not None for _, i in steps):
            title, tests = COMBOS[cid]
            out.append({"combo": cid, "title": title.format(**fmt), "tests": tests,
                        "steps": [{"persona": pid, **_key(i)} for pid, i in steps]})

    if s not in estate.get("lying", []):
        add("lie-then-freeze", [("red-liar", item("red-liar", fault="lying_success", target=s)),
                                ("red-assassin", item("red-assassin", fault="agent_freeze"))], s=s)
        add("lie-then-kill", [("red-liar", item("red-liar", fault="lying_success", target=s)),
                              ("red-assassin", item("red-assassin", fault="agent_kill"))], s=s)
    add("false-log-then-kill", [("red-liar", item("red-liar", fault="false_log", target=root, blame=wrong)),
                                ("red-assassin", item("red-assassin", fault="agent_kill"))], root=root, wrong=wrong)
    if SPOILER.get(s) and "schema_drift" not in (estate.get("weak", {}).get("unix") or []):
        add("break-then-drift", [("red-saboteur", item("red-saboteur", fault=SPOILER[s], target=s)),
                                 ("red-drifter", item("red-drifter", fault="schema_drift", target="unix"))], s=s)
    hack = next((m for m in menus.get("red-hacker", [])), None)
    if hack:
        add("kill-then-exploit", [("red-assassin", item("red-assassin", fault="agent_kill")),
                                  ("red-hacker", hack)], vuln=VULN_OF.get(hack["fault"], hack["fault"]))
    return out


def policy_pick(options: list[dict], lessons: list[dict]) -> dict | None:
    """Deterministic fallback: the first feasible combo that isn't the last one tried."""
    last = lessons[-1].get("combo") if lessons else None
    return next((o for o in options if o["combo"] != last), options[0] if options else None)


# ------------------------------------------------------------------ orders on the shared board
def my_order(persona_id: str, run_id: str | None) -> dict | None:
    """The Commander's order for this Striker this night (``queued`` = stand by, ``assigned`` = go)."""
    order = (world().red_agents.find_one({"_id": persona_id}, {"order": 1}) or {}).get("order")
    if not order or order.get("status") not in ("queued", "assigned"):
        return None
    if order.get("run_id") != run_id:
        world().red_agents.update_one({"_id": persona_id, "order.campaign": order.get("campaign")},
                                      {"$unset": {"order": ""}})
        return None
    return order


def order_strike(persona_id: str, order: dict, snap: dict, cfg: core.Settings) -> tuple[dict | None, str]:
    """Turn an order into a validated strike from the Striker's OWN menu now (a mid-send victim may have changed)."""
    p = PERSONAS[persona_id]
    menu = p.menu(snap, cfg)
    if order["fault"] in AGENT_FAULTS:   # the ordered victim if still on my menu, else the best worker now
        item = next((m for m in menu if m["fault"] == order["fault"] and m.get("victim") == order.get("victim")
                     and order.get("victim")), None) or _worker_item(menu, order["fault"])
    else:
        want = {k: order[k] for k in KEYS if order.get(k) is not None}
        item = next((m for m in menu if _key(m) == want), None)
    if item is None:
        return None, f"{order['fault']} on {order.get('target')} is not on my menu right now"
    return validate(p, {**_key(item), "reason": order.get("reason", "")}, snap, cfg)


def take_order(persona_id: str, order: dict) -> bool:
    r = world().red_agents.update_one(
        {"_id": persona_id, "order.campaign": order["campaign"], "order.step": order["step"], "order.status": "assigned"},
        {"$set": {"order.status": "taken", "order.taken_at": now()}})
    return r.modified_count == 1


def report_order(persona_id: str, order: dict, event: dict | None) -> None:
    status = "failed" if not event or event.get("status") != "ok" else "done"
    world().red_agents.update_one(
        {"_id": persona_id, "order.campaign": order["campaign"], "order.step": order["step"]},
        {"$set": {"order.status": status, "order.event": (event or {}).get("_id"), "order.done_at": now()}})


# ------------------------------------------------------------------ the Commander
SYSTEM_PROMPT = (
    "You are the Red Commander in a chaos drill against the Hive, a team of AI operations agents keeping a "
    "simulated bank's legacy estate (portal -> unix -> mainframe) online; MongoDB holds all their state. Your "
    "Strikers execute vetted faults; you pick ONE campaign (an ordered combo) from the menu, timed against what "
    "your Scouts report the Hive is doing now and what past attacks taught you. Output ONLY one compact JSON "
    'object, start with \'{\': {"combo": "<menu combo id, exactly>", "reason": "<one sentence, at most 40 words, '
    'explain because>"}')
REFLECT_PROMPT = (
    "You are the Red Commander reflecting on one campaign of a chaos drill. Use only the supplied evidence; do not "
    "invent recovery or claim causality. Return JSON {\"reflection\": \"1-2 sentences: did the Guard catch it, did a "
    "Nurse take over, how fast was recovery\", \"lesson\": \"specific next combo or timing change\"}.")


class CmdState(TypedDict, total=False):
    snap: dict
    findings: dict
    mode: str          # hold | plan | advance | reflect
    hold: str | None
    pick: dict | None
    planner: str


class Commander:
    def __init__(self, cfg: core.Settings | None = None):
        self.cfg = cfg or core.Settings.from_env()
        self.cooldown_s = _f("RED_COMMANDER_COOLDOWN_S", 45)
        self.first_s = _f("RED_COMMANDER_FIRST_S", 20)
        self.step_gap_s = _f("RED_COMMANDER_STEP_GAP_S", 3)
        self.step_timeout_s = _f("RED_COMMANDER_STEP_TIMEOUT_S", 90)
        self.observe_s = _f("RED_COMMANDER_OBSERVE_S", 45)
        self.max_campaigns = int(_f("RED_COMMANDER_MAX", 0))
        self.llm_calls = 0
        self.fallback_count = 0
        self._restored = False
        self._stop = threading.Event()
        self._beat_at = 0.0
        self.graph = self._build()

    # ---------------------------------------------------------- board
    def _doc(self) -> dict:
        return world().red_agents.find_one({"_id": COMMANDER_ID}) or {}

    def _set(self, **fields) -> None:
        world().red_agents.update_one({"_id": COMMANDER_ID}, {"$set": fields}, upsert=True)

    def _say(self, text: str, kind: str = "thought", system: str | None = None) -> None:
        """Thoughts are capped at 280 chars: a long reasoning line goes out as up to 3 lines, not truncated."""
        parts, rest = [], text
        while rest and len(parts) < 3:
            if len(rest) <= 278:
                parts.append(rest)
                break
            cut = rest.rfind(" ", 0, 272)
            parts.append(rest[:cut if cut > 100 else 272] + " …")
            rest = "… " + rest[cut if cut > 100 else 272:].lstrip()
        for p in parts:
            think(COMMANDER_ID, core.ROLE, p, system=system, kind=kind)

    def _label(self, planner: str) -> str:
        return "claude" if planner == "claude" else "policy (no reasoning)"

    def _counts(self) -> str:
        return f"Claude calls={self.llm_calls}; fallbacks={self.fallback_count}."

    # ---------------------------------------------------------- OBSERVE
    def _observe(self, state: CmdState) -> CmdState:
        doc = self._doc()
        if not self._restored:
            self._restored = True
            self.llm_calls, self.fallback_count = doc.get("llm_calls", 0), doc.get("fallback_count", 0)
        snap = core.scout(COMMANDER_ID)
        findings = post_findings(snap)
        camp = doc.get("campaign")
        if camp and camp.get("run_id") != snap.get("run_id"):
            self._cancel_orders(camp)   # reset or a new night: drop the old campaign, never its lesson on new ids
            world().red_agents.update_one({"_id": COMMANDER_ID}, {"$unset": {"campaign": ""}})
            camp = None
        if camp and camp["status"] == "active":
            return {"snap": snap, "findings": findings, "mode": "advance"}
        if camp and camp["status"] in ("observing", "aborted"):
            ready = camp["status"] == "aborted" or (now() - camp["observe_from"]).total_seconds() >= self.observe_s
            return {"snap": snap, "findings": findings, "mode": "reflect" if ready else "hold",
                    "hold": None if ready else "watching the Hive defend"}
        hold = self._gate(snap, doc)
        return {"snap": snap, "findings": findings, "mode": "hold" if hold else "plan", "hold": hold}

    def _gate(self, snap: dict, doc: dict) -> str | None:
        if not snap.get("night_running"):
            return "no night running"
        if snap.get("scenario") in self.cfg.skip_scenarios:
            return f"{snap['scenario']} is off limits"
        if snap.get("upgrade_busy"):
            return "the Gardener is mid-upgrade"
        history = [h for h in doc.get("lessons") or [] if h.get("run_id") == snap.get("run_id")]
        if self.max_campaigns and len(history) >= self.max_campaigns:
            return f"campaign budget spent ({len(history)}/{self.max_campaigns})"
        started = snap.get("night_started")
        if started and (now() - started).total_seconds() < self.first_s:
            return "letting the night unfold"
        last = history[-1].get("ts") if history else None
        if last and (now() - last).total_seconds() < self.cooldown_s:
            return "regrouping after the last campaign"
        return None

    # ---------------------------------------------------------- THINK
    def _think(self, state: CmdState) -> CmdState:
        snap = state["snap"]
        options = combos(snap, self.cfg, state.get("findings"))
        if not options:
            return {"pick": None, "hold": "no feasible combo right now"}
        lessons = self._doc().get("lessons") or []
        pick, planner, rejected = None, "policy", None
        if self.cfg.use_llm and llm.llm_available():
            self.llm_calls += 1
            prompt = json.dumps({
                "scout_reports": state.get("findings"),
                "my_past_campaigns_and_lessons": [{k: h.get(k) for k in ("combo", "outcome_summary", "reflection", "lesson")}
                                                  for h in lessons[-4:]],
                "recent_attack_outcomes": snap.get("attack_history", []),
                "scenario": snap.get("scenario"),
                "campaign_menu": [{"combo": o["combo"], "plan": o["title"], "tests": o["tests"],
                                   "steps": o["steps"]} for o in options],
            }, default=str)
            choice = core.parse_json(llm.complete(SYSTEM_PROMPT, prompt, max_tokens=400))
            if choice is not None:
                pick = next((o for o in options if o["combo"] == choice.get("combo")), None)
                if pick is not None:
                    reason = choice.get("reason") if isinstance(choice.get("reason"), str) else ""
                    pick, planner = {**pick, "reason": reason.strip()[:500] or pick["tests"]}, "claude"
                else:
                    rejected = f"Claude proposed {choice.get('combo')!r}: not a feasible combo"
        if pick is None:
            self.fallback_count += 1
            if rejected:
                self._say(f"Red Commander: {rejected}; using my own playbook.", kind="reject")
            p = policy_pick(options, lessons)
            pick = {**p, "reason": f"{p['tests']}; scouts say next steps land on "
                                   f"{', '.join((state.get('findings') or {}).get(SCOUT_HIVE, {}).get('pending_step_systems') or []) or 'nothing yet'}"}
        return {"pick": pick, "planner": planner}

    # ---------------------------------------------------------- ACT
    def _launch(self, state: CmdState) -> CmdState:
        pick, snap = state["pick"], state["snap"]
        cid = f"CMP-{snap['run_id']}-{int(time.time())}"
        steps = [{**s, "status": "queued", "event": None} for s in pick["steps"]]
        camp = {"id": cid, "run_id": snap["run_id"], "combo": pick["combo"], "title": pick["title"],
                "tests": pick["tests"], "reason": pick["reason"], "planner": state["planner"], "steps": steps,
                "status": "active", "current": 0, "started_at": now()}
        self._set(campaign=camp, planner=self._label(state["planner"]))
        plan = " -> ".join(f"{s['persona'].removeprefix('red-')}: {s['fault']}" for s in steps)
        self._say(f"Red Commander ({self._label(state['planner'])}) THINK: campaign {pick['combo']}: {plan}. "
                  f"{pick['reason']} {self._counts()}")
        for i, s in enumerate(steps):
            self._order(camp, i, "queued")
        self._assign(camp, 0)
        return {}

    def _order(self, camp: dict, i: int, status: str) -> None:
        s = camp["steps"][i]
        order = {**{k: s[k] for k in ("fault", "target", "victim", "blame", "to") if s.get(k) is not None},
                 "campaign": camp["id"], "run_id": camp["run_id"], "combo": camp["combo"], "step": i,
                 "of": len(camp["steps"]), "status": status, "reason": f"Commander campaign {camp['combo']}: {camp['reason']}",
                 "ts": now()}
        if status == "queued":   # a later step never overwrites an earlier one for the same Striker
            world().red_agents.update_one({"_id": s["persona"], "order.status": {"$nin": ["queued", "assigned", "taken"]}},
                                          {"$set": {"order": order}})
            world().red_agents.update_one({"_id": s["persona"], "order": {"$exists": False}}, {"$set": {"order": order}})
        else:
            world().red_agents.update_one({"_id": s["persona"]}, {"$set": {"order": order}}, upsert=True)

    def _assign(self, camp: dict, i: int) -> None:
        self._order(camp, i, "assigned")
        camp["steps"][i].update(status="assigned", assigned_at=now())
        camp["current"] = i
        self._set(campaign=camp)
        s = camp["steps"][i]
        self._say(f"Red Commander ACT: step {i + 1}/{len(camp['steps'])} -> {s['persona']}: {s['fault']} on "
                  f"{s.get('victim') or s['target']}.", kind="action")

    def _advance(self, state: CmdState) -> CmdState:
        camp = self._doc().get("campaign")
        if not camp or camp.get("status") != "active":
            return {}
        i = camp["current"]
        step = camp["steps"][i]
        order = (world().red_agents.find_one({"_id": step["persona"]}, {"order": 1}) or {}).get("order") or {}
        mine = order.get("campaign") == camp["id"] and order.get("step") == i
        status = order.get("status") if mine else "lost"
        if status in ("done", "failed"):
            step.update(status=status, event=order.get("event"))
            ev = world().chaos_events.find_one({"_id": order.get("event")}) if order.get("event") else None
            effect = ((ev or {}).get("result") or {}).get("effect") or ((ev or {}).get("result") or {}).get("error")
            self._say(f"Red Commander OBSERVE: step {i + 1} {'landed' if status == 'done' else 'missed'} "
                      f"({order.get('event')}): {effect or step['fault']}.", kind="action")
            if status == "failed":
                return self._close(camp, "aborted")
            if i + 1 < len(camp["steps"]):
                camp["steps"][i]["done_at"] = now()
                self._set(campaign=camp)
                if self.step_gap_s:
                    time.sleep(min(self.step_gap_s, 10))
                self._assign(camp, i + 1)
                return {}
            return self._close(camp, "observing")
        age = (now() - step.get("assigned_at", now())).total_seconds()
        if status == "lost" or (status == "assigned" and age > self.step_timeout_s) or age > self.step_timeout_s + 40:
            step["status"] = "expired"
            self._say(f"Red Commander: step {i + 1} ({step['persona']}: {step['fault']}) never went out; "
                      "calling the campaign off.", kind="reject")
            return self._close(camp, "aborted")
        return {}

    def _close(self, camp: dict, status: str) -> CmdState:
        self._cancel_orders(camp)
        camp.update(status=status, observe_from=now())
        self._set(campaign=camp)
        return {}

    def _cancel_orders(self, camp: dict) -> None:
        for s in camp.get("steps") or []:
            world().red_agents.update_one({"_id": s["persona"], "order.campaign": camp["id"],
                                           "order.status": {"$in": ["queued", "assigned"]}}, {"$unset": {"order": ""}})

    # ---------------------------------------------------------- REFLECT
    def _reflect(self, state: CmdState) -> CmdState:
        camp = self._doc().get("campaign")
        if not camp:
            return {}
        events = [world().chaos_events.find_one({"_id": s["event"]}) for s in camp["steps"] if s.get("event")]
        events = [e for e in events if e]
        evidence = [{"step": i + 1, "fault": e["fault"], "target": e.get("target"), "status": e.get("status"),
                     "effect": (e.get("result") or {}).get("effect") or (e.get("result") or {}).get("error"),
                     "outcome": learning.outcome(e)} for i, e in enumerate(events)]
        caught = any(ev["outcome"].get("guard_caught") for ev in evidence)
        recov = [ev["outcome"]["recovery_s"] for ev in evidence if ev["outcome"].get("recovery_s") is not None]
        seen = {r["incident"]: r for ev in evidence for r in ev["outcome"].get("incidents") or []}
        handoffs = sum(len(r.get("handoffs") or []) for r in seen.values())
        summary = {"landed": f"{len(events)}/{len(camp['steps'])}", "guard_caught": caught or None,
                   "recovery_s": max(recov) if recov else None, "nurse_handoffs": handoffs,
                   "aborted": camp["status"] == "aborted"}
        reflection = (f"{summary['landed']} steps landed; Guard catch {'seen' if caught else 'not observed'}; "
                      f"{handoffs} handoffs; recovery {'%.1fs' % summary['recovery_s'] if recov else 'not observed'}.")
        lesson = ("Repeat the combo with tighter timing." if not caught and not recov
                  else "The Hive defended this combo; switch to a different pairing next.")
        planner = "policy"
        if self.cfg.use_llm and llm.llm_available():
            self.llm_calls += 1
            reply = core.parse_json(llm.complete(REFLECT_PROMPT, llm.fence(
                "campaign and Hive read-back",
                json.dumps({"campaign": {k: camp[k] for k in ("combo", "title", "tests", "reason")},
                            "summary": summary, "steps": evidence}, default=str), 12000), max_tokens=450))
            if reply and all(isinstance(reply.get(k), str) and reply[k].strip() for k in ("reflection", "lesson")):
                reflection, lesson, planner = reply["reflection"][:800], reply["lesson"][:400], "claude"
            else:
                self.fallback_count += 1
        else:
            self.fallback_count += 1
        row = {"campaign": camp["id"], "run_id": camp["run_id"], "combo": camp["combo"], "planner": planner,
               "events": [e["_id"] for e in events], "outcome_summary": summary, "reflection": reflection,
               "lesson": lesson, "ts": now()}
        for e in events:   # the campaign's verdict lives with the attacks in chaos_events
            world().chaos_events.update_one({"_id": e["_id"], "ts": e["ts"]}, {"$set": {"red.campaign_reflection": row}})
        world().red_agents.update_one({"_id": COMMANDER_ID}, {"$set": {"last_reflection": row},
                                                              "$unset": {"campaign": ""},
                                                              "$push": {"lessons": {"$each": [row], "$slice": -8}}})
        self._say(f"Red Commander REFLECT ({self._label(planner)}): {reflection}", kind="action")
        self._say(f"Red Commander LEARN: {lesson} {self._counts()}", kind="action")
        return {}

    # ---------------------------------------------------------- graph + loop
    def _build(self):
        from langgraph.graph import END, START, StateGraph

        g = StateGraph(CmdState)
        g.add_node("observe", self._observe)
        g.add_node("think", self._think)
        g.add_node("act", self._launch)
        g.add_node("advance", self._advance)
        g.add_node("reflect", self._reflect)
        g.add_edge(START, "observe")
        g.add_conditional_edges("observe", lambda s: {"plan": "think", "advance": "advance",
                                                      "reflect": "reflect"}.get(s.get("mode"), END))
        g.add_conditional_edges("think", lambda s: "act" if s.get("pick") else END)
        g.add_edge("act", END)
        g.add_edge("advance", END)
        g.add_edge("reflect", END)
        return g.compile()

    def step(self) -> CmdState:
        out = self.graph.invoke({})
        status = {"plan": "planning", "advance": "commanding", "reflect": "reflecting"}.get(out.get("mode"), "holding")
        if out.get("mode") != "hold" or time.monotonic() - self._beat_at > 5:
            self._beat_at = time.monotonic()
            self._set(name="Red Commander", kind="commander", status=status, hold=out.get("hold"), pid=os.getpid(),
                      last_seen=now(), llm_calls=self.llm_calls, fallback_count=self.fallback_count)
        return out

    def run(self) -> None:
        self._say("Red Commander online: plans campaigns of vetted faults for the Strikers; Hive and Systems "
                  "Scouts report in.")
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("commander tick failed")
            self._stop.wait(self.cfg.poll_s)
        try:
            self._set(status="stopped", last_seen=now())
        except Exception:
            pass

    def stop(self, *_):
        self._stop.set()


def start_thread() -> Commander:
    c = Commander()
    threading.Thread(target=c.run, name="red-commander", daemon=True).start()
    return c
