"""One Red attacker: a LangGraph loop  scout -> plan -> strike -> observe  for a single persona.

Every strike goes through ``chaos.runner.fire`` (so it is an ordinary ``world.chaos_events`` document,
tagged ``by: <persona id>``). Each attacker heartbeats into ``world.red_agents`` so the war room can show
the fleet.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import TypedDict

from common import llm, pause
from common.db import now, world
from common.events import think

from .. import runner
from . import commander, core, learning
from .personas import KEYS, Persona, validate

log = logging.getLogger("chaos.redfleet")

COMMON_PROMPT = (
    "You are one attacker in the Red fleet, a chaos drill against the Hive: a team of AI operations agents "
    "keeping a simulated bank's legacy estate online (portal -> unix -> mainframe; MongoDB holds all their "
    "state). The drill tests the problem statement 'Finish the Job': can the Hive finish a long multi-step job "
    "across systems that won't hold still? Q1 tell an action that took effect from one only sent; Q2 resume "
    "after a crash without replaying its history; Q3 adapt when a system's layout, fields or workflow change. "
    "Cause as much trouble as you can with ONE pick from YOUR menu, timed against what the Hive is doing now. "
    "Output ONLY one compact JSON object and nothing else (no prose, no code fence; start with '{'): the chosen "
    'menu item\'s fields copied exactly, plus "reason": "<one sentence, at most 40 words, explain because>".'
)


class RedState(TypedDict, total=False):
    snap: dict
    hold: str | None
    strike: dict | None
    planner: str
    event: dict | None
    order: dict | None


class Attacker:
    def __init__(self, persona: Persona, cfg: core.Settings | None = None):
        self.p = persona
        self.cfg = cfg or core.Settings.from_env()
        self.graph = self._build()
        self._stop = False
        self._beat: tuple = ()
        self._beat_at = 0.0
        self.llm_calls = 0
        self.fallback_count = 0
        self._pending = None
        self._restored = False

    # -------------------------------------------------------------- planning
    def plan(self, snap: dict) -> tuple[dict | None, str, str | None]:
        """(validated strike | None, "claude"|"policy", rejection note | None)."""
        menu = self.p.menu(snap, self.cfg)
        if not menu:
            return None, "policy", None
        rejected = None
        if self.cfg.use_llm and llm.llm_available():
            items = [{k: m[k] for k in KEYS if k in m} | ({"role": m["role"], "phase": m.get("phase")}
                                                          if "role" in m else {}) for m in menu]
            prompt = (f"You are the {self.p.name}: {self.p.motto}. Your angle: {self.p.question}.\n"
                      f"State:\n{core.brief(snap)}\n\nYour menu:\n{json.dumps(items)}")
            self.llm_calls += 1
            choice = core.parse_json(llm.complete(COMMON_PROMPT, prompt, max_tokens=500))
            if choice is not None:
                choice = {k: v for k, v in choice.items() if k in KEYS or k == "reason"}
                ok, why = validate(self.p, choice, snap, self.cfg)
                if ok:
                    ok["reason"] = ok["reason"] or "maximum trouble for the Hive"
                    return ok, "claude", None
                rejected = f"Claude proposed {choice.get('fault')!r} on {choice.get('target')!r}: rejected ({why})"
        self.fallback_count += 1
        lessons = snap.get("lessons") or []
        last_fault = lessons[-1].get("fault") if lessons else None
        alternative = [m for m in menu if m["fault"] != last_fault]
        fallback = self.p.policy_fn(snap, alternative or menu)
        if fallback is None:
            return None, "policy", rejected
        ok, _ = validate(self.p, fallback, snap, self.cfg)
        return ok, "policy", rejected

    # -------------------------------------------------------------- graph nodes
    def _scout(self, state: RedState) -> RedState:
        if not self._restored:
            self._restored = True
            reg = world().red_agents.find_one({"_id": self.p.id}) or {}
            self.llm_calls = reg.get("llm_calls", 0)
            self.fallback_count = reg.get("fallback_count", 0)
            self._pending = reg.get("pending_reflection")
        self._finish_reflection()
        snap = core.scout(self.p.id)
        order = commander.my_order(self.p.id, snap) if commander.enabled() else None
        if order:
            return {"snap": snap, "order": order, "hold": self._order_gate(snap, order)}
        return {"snap": snap, "hold": ("observing Hive defence" if self._pending else core.gate(snap, self.cfg, self.p.stagger_s))}

    def _order_gate(self, snap: dict, order: dict) -> str | None:
        """A Commander order skips my own cooldown, but never the no-permanent-damage rules."""
        if order["status"] == "queued":
            return f"standing by for Commander campaign {order['combo']} (step {order['step'] + 1}/{order['of']})"
        if not snap.get("night_running"):
            return "no night running"
        if snap.get("upgrade_busy"):
            return "the Gardener is mid-upgrade"
        if any(a.get("status") == "firing" for a in snap.get("red_attacks") or []):
            return "my last attack is still in flight"
        return None

    def _plan(self, state: RedState) -> RedState:
        if state.get("order"):
            return self._plan_order(state)
        strike, planner, rejected = self.plan(state["snap"])
        if rejected:
            think(self.p.id, core.ROLE, f"{self.p.name}: {rejected}; using my own playbook.", kind="reject")
        if strike is None:
            return {"strike": None, "planner": planner}
        label = "claude" if planner == "claude" else "policy (no reasoning)"
        world().red_agents.update_one({"_id": self.p.id}, {"$set": {"planner": label,
            "intent": {"fault": strike["fault"], "target": strike["target"], "ts": now()}}}, upsert=True)
        where = strike.get("victim") or strike["target"]
        extra = f" blaming {strike['params']['blame']}" if strike["fault"] == "false_log" else ""
        think(self.p.id, core.ROLE, f"{self.p.name} ({label}): {strike['fault']} on {where}{extra}. "
                                    f"{strike['reason']} [{strike['question']}] "
                                    f"Claude calls={self.llm_calls}; fallbacks={self.fallback_count}.",
              system=strike["target"] if strike["target"] != "swarm" else None)
        return {"strike": strike, "planner": planner}

    def _plan_order(self, state: RedState) -> RedState:
        """Execute the Commander's step from my own menu (params from the menu, never the model)."""
        order, snap = state["order"], state["snap"]
        strike, why = commander.order_strike(self.p.id, order, snap, self.cfg)
        if strike is None:
            self._heartbeat("holding", f"waiting to execute Commander step: {why}")
            return {"strike": None, "planner": "commander"}
        if not commander.take_order(self.p.id, order):
            return {"strike": None, "planner": "commander"}
        if self._pending:
            self._finish_reflection(force=True)   # reflect early: the next strike would overwrite it
        world().red_agents.update_one({"_id": self.p.id}, {"$set": {"planner": "commander",
            "intent": {"fault": strike["fault"], "target": strike["target"], "ts": now()}}}, upsert=True)
        where = strike.get("victim") or strike["target"]
        think(self.p.id, core.ROLE, f"{self.p.name} (on Red Commander's orders, {order['combo']} step "
                                    f"{order['step'] + 1}/{order['of']}): {strike['fault']} on {where}.",
              system=strike["target"] if strike["target"] != "swarm" else None)
        return {"strike": strike, "planner": "commander"}

    def _strike(self, state: RedState) -> RedState:
        s, snap, order = state["strike"], state["snap"], state.get("order")
        self._heartbeat("striking", None, force=True)
        red = {"persona": self.p.name, "planner": state.get("planner"), "question": s["question"], "reason": s["reason"],
               "incident_ids": [i["id"] for i in snap.get("incidents", [])]}
        if order:
            red["campaign"] = {"id": order["campaign"], "step": order["step"] + 1, "of": order["of"],
                               "combo": order["combo"], "ordered_by": commander.COMMANDER_ID}
        try:
            ev = runner.fire(s["fault"], s["target"], s["params"], manual=False, run_id=snap["run_id"],
                             extra={"by": self.p.id, "red": red})
        except Exception as e:  # never crash an attacker on one bad strike
            log.exception("strike failed")
            think(self.p.id, core.ROLE, f"{self.p.name}: {s['fault']} misfired ({type(e).__name__}).", kind="reject")
            if order:
                commander.report_order(self.p.id, order, None)
            return {"event": None}
        if order:
            commander.report_order(self.p.id, order, ev)
        return {"event": ev}

    def _observe(self, state: RedState) -> RedState:
        ev = state.get("event")
        if not ev:
            return {}
        res = ev.get("result") or {}
        if ev.get("status") == "ok":
            text = f"{self.p.name}: hit landed ({ev['_id']}): {res.get('effect') or ev['fault']}."
        else:
            text = f"{self.p.name}: {ev['fault']} missed ({ev['_id']}): {res.get('error') or res.get('effect') or 'failed'}."
        think(self.p.id, core.ROLE, text, system=ev.get("target") if ev.get("target") != "swarm" else None,
              kind="action")
        world().red_agents.update_one({"_id": self.p.id}, {"$set": {
            "last_attack": {"event": ev["_id"], "fault": ev["fault"], "target": ev.get("target"),
                            "status": ev.get("status"), "ts": ev.get("ts")}}, "$inc": {"attacks": 1}}, upsert=True)
        self._pending = ev
        world().red_agents.update_one({"_id": self.p.id}, {"$set": {"pending_reflection": ev}})
        return {}

    def _reflect(self, state: RedState) -> RedState:
        # Reflection samples the Hive after a bounded observation window, without blocking other ticks.
        self._finish_reflection()
        return {}

    def _finish_reflection(self, force: bool = False):
        ev = self._pending
        if not ev or (not force and (now() - ev["ts"]).total_seconds() < float(os.environ.get("RED_FLEET_REFLECT_S", "12"))):
            return
        if not world().chaos_events.find_one({"_id": ev["_id"], "ts": ev["ts"]}):
            self._pending = None  # reset erased this attack; never attach its lesson to a reused CHX id
            world().red_agents.update_one({"_id": self.p.id}, {"$unset": {"pending_reflection": "", "intent": ""}})
            return
        evidence = learning.outcome(ev)
        reflection = "Observed Hive read-backs are recorded; recovery remains unknown where no resolution is visible."
        lesson = "Use observed Guard failures and takeovers to choose the next vetted template; never infer success from an ACK."
        planner = "policy (no reasoning)"
        if self.cfg.use_llm and llm.llm_available():
            self.llm_calls += 1
            reply = core.parse_json(llm.complete(
                'Reflect on this chaos drill using only supplied evidence. Do not invent recovery or claim causality. '
                'Return JSON {"reflection": "1-2 sentences", "lesson": "specific next timing or template change"}.',
                llm.fence("attack and Hive read-back", json.dumps({"attack": ev, "evidence": evidence}, default=str), 12000),
                max_tokens=450))
            if reply and all(isinstance(reply.get(k), str) and reply[k].strip() for k in ("reflection", "lesson")):
                reflection, lesson, planner = reply["reflection"][:1000], reply["lesson"][:600], "claude"
            else:
                self.fallback_count += 1
        else:
            self.fallback_count += 1
        learning.remember(self.p.id, ev, evidence, reflection, lesson, planner)
        think(self.p.id, core.ROLE, f"{self.p.name} REFLECT ({planner}): {reflection} LEARN: {lesson} "
              f"Claude calls={self.llm_calls}; fallbacks={self.fallback_count}.", kind="action")
        self._pending = None
        world().red_agents.update_one({"_id": self.p.id}, {"$unset": {"pending_reflection": "", "intent": ""}})

    def _build(self):
        from langgraph.graph import END, START, StateGraph

        g = StateGraph(RedState)
        g.add_node("scout", self._scout)
        g.add_node("plan", self._plan)
        g.add_node("strike", self._strike)
        g.add_node("observe", self._observe)
        g.add_node("reflect", self._reflect)
        g.add_edge(START, "scout")
        g.add_conditional_edges("scout", lambda s: END if s.get("hold") else "plan")
        g.add_conditional_edges("plan", lambda s: "strike" if s.get("strike") else END)
        g.add_edge("strike", "observe")
        g.add_edge("observe", "reflect")
        g.add_edge("reflect", END)
        return g.compile()

    # -------------------------------------------------------------- registry + loop
    def _heartbeat(self, status: str, hold: str | None, force: bool = False) -> None:
        beat = (status, hold)
        if not force and beat == self._beat and time.monotonic() - self._beat_at < 5:
            return
        self._beat, self._beat_at = beat, time.monotonic()
        world().red_agents.update_one({"_id": self.p.id}, {"$set": {
            "name": self.p.name, "motto": self.p.motto, "question": self.p.question, "pid": os.getpid(),
            "status": status, "hold": hold, "last_seen": now(),
            "llm_calls": self.llm_calls, "fallback_count": self.fallback_count}}, upsert=True)

    def step(self) -> RedState:
        out = self.graph.invoke({})
        if out.get("hold"):
            self._heartbeat("holding", out["hold"])
        else:
            self._heartbeat("hunting", None, force=bool(out.get("event")))
        return out

    def run(self) -> None:
        think(self.p.id, core.ROLE, f"{self.p.name} online: {self.p.motto}.")
        while not self._stop:
            # GARDENER_PAUSE=1: hold fire while the Gardener upgrades a system (a lease: if the Gardener
            # dies mid-upgrade it lapses and we resume on our own).
            if pause.wait_while_paused(lambda: self._stop, on_pause=lambda d: self._heartbeat(
                    "paused", f"Gardener upgrading {d.get('system')}", force=True)):
                continue
            try:
                self.step()
            except Exception:  # MongoDB blips etc.: crash-only, try again next tick
                log.exception("%s tick failed", self.p.id)
            time.sleep(self.cfg.poll_s)
        try:
            world().red_agents.update_one({"_id": self.p.id}, {"$set": {"status": "stopped", "last_seen": now()}})
        except Exception:
            pass

    def stop(self, *_):
        self._stop = True
