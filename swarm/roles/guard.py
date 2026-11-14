"""Guard bee — the ONLY agent that may mark a step 'verified'.

It never trusts an op's ACK. For every attempt in 'sent' it reads back /state (+ /health, + a security
probe for security fixes) and checks the op's postcondition from the CONTRACTS ops table:

    applied                         -> verified, evidence = the concrete read-back facts
    ACK applied:true, state unchanged -> reject: "ACK ≠ APPLIED" (attempt failed; worker opens a new one)
    accepted, applied:false (slow)  -> keep re-checking up to SLOW_APPLY_S + 10 s, then reject

A change stream on swarm.incidents wakes the guard the moment a step is sent; a periodic sweep backs it up.
Each judgement is a tiny LangGraph run (read_back -> judge) on thread "<action_id>:guard".
"""
from __future__ import annotations

import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from common import config as shared
from common.db import col, now

from .. import schema, settings, steps, verify
from ..harness import Agent, checkpointer, log
from .. import lessons, platform

SENT_STEPS = [
    {"$match": {"active": True, "steps.status": "sent"}},
    {"$unwind": "$steps"},
    {"$match": {"steps.status": "sent"}},
    {"$project": {"_id": 0, "incident": "$_id", "step": "$steps"}},
]


class GuardState(TypedDict, total=False):
    incident: str
    step_id: str
    action_id: str
    applied: bool
    facts: str
    health: str
    verdict: str


def _age(ts) -> float:
    return (now() - ts).total_seconds() if ts else 0.0


def build_graph(agent: Agent):
    sysc = agent.systems
    seen: set[str] = set()       # attempts we've announced reading back
    noted_slow: set[str] = set()

    def current(state) -> dict | None:
        st = steps.get_step(state["incident"], state["step_id"])
        if st and st["status"] == "sent" and st.get("action_id") == state["action_id"]:
            return st
        return None

    def grace(st: dict) -> float:
        """A step whose system lied about this op before (failure memory) gets no benefit of the doubt."""
        if st.get("distrust_ack") and lessons.enabled():
            return min(settings.GUARD_GRACE_S, 0.5)
        return settings.GUARD_GRACE_S

    def remember(inc: str, st: dict, aid: str, evidence: str) -> None:
        """Failure memory: a rejected attempt becomes a durable lesson tagged with the system version."""
        platform.judged(agent, inc, st, False)
        if not (lessons.enabled() or platform.enabled()):
            return
        try:
            doc = col("incidents").find_one({"_id": inc}, {"fault_class": 1}) or {}
            ls = lessons.record_failure(inc, st, aid, evidence, agent.id, sysc.version(st["system"]),
                                        doc.get("fault_class"))
            agent.think(f"Remembered {ls['_id']} (seen {ls.get('count', 1)}×): {ls['why'][:120]}.", kind="learn",
                        incident=inc, system=st["system"], pause=False)
        except Exception as e:
            log(agent.id, f"lesson not stored: {type(e).__name__}: {e}")

    def read_back(state: GuardState, config) -> dict:
        st = current(state)
        if st is None:
            return {"verdict": "moot"}
        aid = state["action_id"]
        agent.set(status="working", target=st["system"], phase="verifying", incident=state["incident"])
        if aid not in seen:
            seen.add(aid)
            agent.think(f"Reading back {st['system']} to check {st['op']} ({aid}).", kind="verify",
                        incident=state["incident"], system=st["system"])
        cache: dict[str, dict] = {}

        def read(sys: str) -> dict:
            if sys not in cache:
                cache[sys] = sysc.state(sys)
            return cache[sys]

        applied, facts = verify.check(st["op"], st.get("params") or {}, read, probe=sysc.probe,
                                      sent_at=st.get("sent_at"))
        health = sysc.health(st["system"]).get("status", "unknown")
        return {"applied": applied, "facts": facts, "health": health}

    def judge(state: GuardState, config) -> dict:
        if state.get("verdict") == "moot":
            return {}
        st = current(state)
        if st is None:
            return {"verdict": "moot"}
        inc, aid, op, sys = state["incident"], state["action_id"], st["op"], st["system"]
        facts = state["facts"]
        if state["applied"]:
            if steps.verify(inc, st, aid, f"{facts}; health.{sys}={state['health']}", agent.id):
                agent.think(f"Verified {aid}: {facts}.", kind="verify", incident=inc, system=sys, pause=False)
                platform.judged(agent, inc, st, True)
                if op == "map_field":
                    learn_mapping(inc, st, facts)
                return {"verdict": "verified"}
            return {"verdict": "moot"}
        resp = st.get("response")
        reason = None
        if resp is None:
            # Marked sent but no ACK recorded yet (worker may have died before POSTing): a nurse will
            # reconcile and resend the same action_id. Only give up if it has been stuck a long time.
            if _age(st.get("sent_at")) > 3 * shared.LEASE_S + shared.SLOW_APPLY_S + 10:
                reason = f"no ACK recorded for {aid} and state shows {facts}"
        elif resp.get("error"):
            if _age(st.get("responded_at")) >= settings.GUARD_GRACE_S:
                reason = f"no ACK from {sys} ({resp['error'][:60]}); state shows {facts}"
        elif resp.get("accepted") is False:
            reason = f"{sys} refused {op}: {resp.get('message', '')}"[:200]
        elif resp.get("applied"):
            if _age(st.get("responded_at")) >= grace(st):
                agent.think(f"ACK ≠ APPLIED: {op} returned applied:true but {facts}.", kind="reject",
                            incident=inc, system=sys, pause=False)
                if st.get("distrust_ack") and lessons.enabled():
                    agent.think(f"{sys} lied about {op} before ({(st.get('lessons') or [{}])[0].get('id')}); rejected on sight, "
                                f"no grace period.", kind="reject", incident=inc, system=sys, pause=False)
                if steps.reject(inc, st, aid, f"ACK ≠ APPLIED: {facts}", agent.id):
                    remember(inc, st, aid, f"ACK ≠ APPLIED: {facts}")
                    return {"verdict": "rejected"}
                return {"verdict": "moot"}
        else:  # accepted, not applied yet: accepted is not done
            if _age(st.get("sent_at")) > shared.SLOW_APPLY_S + 10:
                reason = f"{op} accepted but never applied after {int(_age(st.get('sent_at')))}s: {facts}"
            elif aid not in noted_slow:
                noted_slow.add(aid)
                agent.think(f"Accepted is not done: {op} on {sys} hasn't landed yet ({facts}); re-checking.",
                            kind="verify", incident=inc, system=sys, pause=False)
        if reason:
            agent.think(f"Rejecting {aid}: {reason}.", kind="reject", incident=inc, system=sys, pause=False)
            if steps.reject(inc, st, aid, reason, agent.id):
                remember(inc, st, aid, reason)
                return {"verdict": "rejected"}
        return {"verdict": "pending"}

    def learn_mapping(inc: str, st: dict, facts: str) -> None:
        """A verified field mapping becomes shared memory: swarm.schema_mappings (new version, or the earlier
        version reused when this exact change was verified before). The good sample is re-captured at once."""
        p = st.get("params") or {}
        try:
            unix_state = sysc.state(schema.SYSTEM)
        except Exception:
            unix_state = None
        try:
            m = schema.learn_verified(p.get("field", "balance"), p.get("source"), inc, st["step_id"], facts, agent.id,
                                      unix_state)
        except Exception as e:
            log(agent.id, f"schema mapping not stored: {type(e).__name__}: {e}")
            return
        if not m:
            return
        try:
            doc = schema.guard_verdict(inc, m["to"], facts, m["version"])
        except Exception:
            doc = None
        n = re.search(r"(\d+/\d+) accounts", facts or "")
        lined = f"{n.group(1)} values match" if n else "values line up"
        if m.get("reused"):
            agent.think(f"{schema.SYSTEM} renamed {m['from']} → {m['to']} · seen before: mapping v{m['version']} "
                        f"reused (swarm.schema_mappings history).", kind="learn", incident=inc,
                        system=schema.SYSTEM, pause=False)
        else:
            agent.think(f"{schema.SYSTEM} renamed {m['from']} → {m['to']} · mapping learned v{m['version']} "
                        f"(swarm.schema_mappings; every agent reads {m['to']} from now on).", kind="learn",
                        incident=inc, system=schema.SYSTEM, pause=False)
        agent.think(provenance(inc, m, (doc or {}).get("ai") or {}, lined), kind="learn", incident=inc,
                    system=schema.SYSTEM, pause=False)

    g = StateGraph(GuardState)
    g.add_node("read_back", read_back)
    g.add_node("judge", judge)
    g.add_edge(START, "read_back")
    g.add_edge("read_back", "judge")
    g.add_edge("judge", END)
    return g.compile(checkpointer=checkpointer())


def provenance(inc: str, m: dict, ai: dict, lined: str) -> str:
    """The Guard's chain line, naming who really proposed the mapping (memory / Claude / rules)."""
    how = ai.get("proposed_by")
    if not how:  # flag off: the unix hop's findings still say who proposed it
        t = col("tasks").find_one({"incident": inc, "kind": "diagnose", "findings.params.to": m["to"]},
                                  {"findings.params": 1}, sort=[("ts", -1)])
        how = (((t or {}).get("findings") or {}).get("params") or {}).get("proposed_by")
    stored = f"mapping v{m['version']} {'reused' if m.get('reused') else 'stored'}"
    v0 = (ai.get("verdicts") or [{}])[0]
    claimed = ((ai.get("proposed_ops") or [{}])[0].get("params") or {}).get("source")
    if how == "memory":
        head = f"memory: MongoDB already knew {m['from']}→{m['to']} (no Claude call)"
    elif how == "claude" and v0.get("verdict") == "overruled":
        head = (f"rules: no op for unknown field {m['to']} → Claude proposed {m['from']}→{claimed}, "
                f"rules overruled → {m['to']}")
    elif how == "claude":
        head = f"rules: no op for unknown field {m['to']} → Claude proposed {m['from']}→{m['to']}"
    else:
        head = f"rules: no playbook/op for unknown field {m['to']} → type + value match proposed {m['from']}→{m['to']}"
    return f"{head} → Guard verified ({lined}) → {stored}."


def run(agent: Agent) -> None:
    graph = build_graph(agent)
    busy = False
    while not agent.stop.is_set():
        try:
            # Wake on any incident update (a step being sent), or after POLL_S anyway.
            with col("incidents").watch([{"$match": {"operationType": {"$in": ["update", "replace"]}}}],
                                        max_await_time_ms=int(settings.POLL_S * 1000)) as stream:
                while not agent.stop.is_set():
                    stream.try_next()
                    pending = list(col("incidents").aggregate(SENT_STEPS))
                    for p in pending:
                        st = p["step"]
                        graph.invoke({"incident": p["incident"], "step_id": st["step_id"], "action_id": st["action_id"],
                                      "verdict": None, "applied": False},
                                     {"configurable": {"thread_id": f"{st['action_id']}:guard"}}, durability="exit")
                    if pending:
                        busy = True
                    elif busy:
                        busy = False
                        agent.rest()
                    if not pending:
                        agent.nap(settings.POLL_S / 2)
        except SystemExit:
            raise
        except Exception as e:
            log(agent.id, f"error: {type(e).__name__}: {e}")
            agent.nap(1.0)
