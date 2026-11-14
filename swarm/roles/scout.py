"""Scout — owns one incident end to end (thread "<incident>:scout").

    observe -> recall --usable hit--> confirm --fits--> plan
                 \\                       \\--doesn't fit--\\
                  \\--no hit-----------------------------> diagnose (one hop per run, loops) -> classify -> plan
    plan -> dispatch <-> approve (LangGraph interrupt until the Beekeeper decides)
            dispatch <-> await_group (workers act, guard verifies)
            dispatch -> resolve (all steps verified AND all affected systems healthy) -> playbook

All incident writes are fenced on the incident lease {owner, generation}. If the scout dies, a Nurse
claims the expired lease and resumes this same graph thread from its last MongoDB checkpoint.
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pymongo.errors import DuplicateKeyError

from common import config as shared
from common.db import col, next_id, now, txn
from common.events import say as bus

from .. import leases, policy, recall, recovery, scale, second_opinion, settings, steps

from ..harness import Agent, checkpointer, main_loop, retrying
from .. import lessons


class ScoutState(TypedDict, total=False):
    incident: str
    started: float
    symptom_text: str
    hit: dict | None             # usable recall hit {playbook_id, system, fault_class, fix}
    path: list[str]
    current: str | None
    findings: dict
    diag_started: float
    diag_s: float
    root: str
    fault_class: str
    root_cause: str
    params: dict
    outcome: str


def _who(config) -> tuple[str, int]:
    c = config["configurable"]
    return c["agent"], int(c["generation"])


def scout_config(inc_id: str, agent_id: str, generation: int) -> dict:
    return {"configurable": {"thread_id": f"{inc_id}:scout", "agent": agent_id, "generation": generation},
            "recursion_limit": 500}


def build_graph(agent: Agent):
    sysc = agent.systems

    # ------------------------------------------------------------------ helpers
    def inc_doc(inc_id: str) -> dict:
        return col("incidents").find_one({"_id": inc_id})

    def fenced(config, inc_id: str, update: dict, array_filters=None) -> None:
        """Incident write conditional on our lease (owner + generation)."""
        me, gen = _who(config)
        agent.check("incidents", inc_id)
        r = col("incidents").update_one({"_id": inc_id, "owner": me, "generation": gen}, update,
                                        array_filters=array_filters)
        if r.matched_count != 1:
            raise leases.Fenced(f"incident {inc_id} no longer held by {me} gen {gen}")

    def say(inc_id: str, text: str, kind: str = "thought", system: str | None = None, pause: bool = True,
            polish: bool = False):
        agent.think(text, kind=kind, incident=inc_id, system=system, pause=pause, polish=polish)

    def wait_task(inc_id: str, task_id: str) -> dict:
        deadline = time.monotonic() + settings.TASK_TIMEOUT_S
        while time.monotonic() < deadline:
            agent.check("incidents", inc_id)
            t = col("tasks").find_one({"_id": task_id})
            if t and t["status"] in ("done", "failed"):
                return t
            agent.nap(settings.POLL_S)
        return {"_id": task_id, "status": "failed", "findings": None}

    def new_task(inc: dict, task_id: str, kind: str, specialty: str, **fields) -> None:
        doc = {"_id": task_id, "incident": inc["_id"], "kind": kind, "specialty": specialty,
               "step_ids": [], "status": "open", "owner": None, "lease_until": None,
               "generation": 0, "checkpoint_thread": task_id, "ts": now(), **fields}
        try:
            col("tasks").insert_one(doc)
        except DuplicateKeyError:
            pass  # node re-ran after a resume: the task already exists
        if scale.enabled():  # dynamic scaling: spawn the specialist this task needs if none is free
            try:
                rid = scale.ensure_worker(agent.id, doc)
            except Exception as e:  # never block the incident on scaling; a nurse picks the task up anyway
                rid = None
                agent.think(f"Spawn request for {task_id} failed ({type(e).__name__}); a nurse will cover it.",
                            incident=inc["_id"], pause=False)
            if rid:
                agent.think(f"Spawning a {specialty} worker for {task_id} ({rid}).", kind="spawn",
                            incident=inc["_id"], system=fields.get("system"), pause=False)

    def escalate(config, inc_id: str, reason: str) -> dict:
        fenced(config, inc_id, {"$set": {"status": "failed", "active": False, "closed_at": now(),
                                          "failure": reason}})
        say(inc_id, f"Escalating {inc_id} to the Beekeeper: {reason}", pause=False)
        bus(agent.id, "queen", "escalate", {"incident": inc_id, "reason": reason})
        return {"outcome": "failed"}

    # ------------------------------------------------------------------ nodes
    def observe(state: ScoutState, config) -> dict:
        inc_id = state["incident"]
        inc = inc_doc(inc_id)
        agent.set(status="working", incident=inc_id, target=inc["system"], phase="triage")
        # Let the cascade's blast radius settle: wait until `affected` stops growing — and, if the most
        # downstream affected system feeds a still-healthy dependent, give the failure time to reach it
        # (the customer-facing symptom is where the walk should start).
        last, stable_since, noted = None, time.monotonic(), False
        t_end = time.monotonic() + settings.CASCADE_WAIT_S + 5
        while time.monotonic() < t_end:
            agent.check("incidents", inc_id)
            doc = inc_doc(inc_id)
            cur = sorted(doc.get("affected") or [])
            age = (now() - doc["opened_at"]).total_seconds()
            pending = [] if doc.get("kind") == "security" else \
                [d for d in policy.systems() if shared.CHAIN.get(d) == policy.most_downstream(cur)]
            if cur != last:
                last, stable_since = cur, time.monotonic()
            elif time.monotonic() - stable_since >= settings.SETTLE_S and (not pending or age >= settings.CASCADE_WAIT_S):
                break
            if pending and not noted:
                noted = True
                say(inc_id, f"{policy.most_downstream(cur)} feeds {', '.join(pending)}; letting the blast radius settle "
                            f"before choosing where to start.", system=policy.most_downstream(cur), pause=False)
            agent.nap(settings.POLL_S / 2)
        inc = inc_doc(inc_id)
        fenced(config, inc_id, {"$set": {"status": "diagnosing"}})
        agent.set(target=inc["system"])
        affected = ", ".join(sorted(inc.get("affected") or [], key=policy.rank))
        lead = inc["symptoms"][0] if inc.get("symptoms") else f"{inc['system']} unhealthy"
        say(inc_id, f"{inc_id}: {lead}. Affected: {affected}. Checking the hive's memory first.",
            system=inc["system"])
        return {"started": time.time(), "symptom_text": policy.symptom_text(inc.get("symptoms") or []),
                "path": [], "findings": {}, "hit": None}

    def recall_node(state: ScoutState, config) -> dict:
        inc_id = state["incident"]
        agent.set(phase="recall")
        q = state["symptom_text"]
        flt, hits = recall.search(q)
        usable, summary = recall.evaluate(hits, sysc.version)
        skipped = []
        if lessons.enabled():  # failure memory: a playbook that didn't fit these symptoms before is skipped
            try:
                usable, skipped = lessons.skip_misfits(hits, summary, usable, q, inc_id)
            except Exception:
                skipped = []
        fenced(config, inc_id, {"$set": {"recall": {
            "query": q, "filter": flt, "hits": summary, "used": usable is not None, "model": flt.get("embedding_model"),
            "threshold": shared.RECALL_THRESHOLD, "num_candidates": recall.NUM_CANDIDATES, "limit": recall.LIMIT,
            "ts": now()}}})
        inc = inc_doc(inc_id)
        for line in skipped:
            say(inc_id, line, kind="recall", system=inc["system"], pause=False)
        if usable:
            say(inc_id, f"Recall hit {usable['_id']} (score {usable['score']:.2f}, {usable['system']} "
                        f"v{usable['system_version']}): {usable['fault_class']} — skipping the chain walk.",
                kind="recall", system=usable["system"])
            return {"hit": {"playbook_id": usable["_id"], "system": usable["system"],
                            "fault_class": usable["fault_class"], "fix": usable.get("fix") or []},
                    "diag_started": time.time(), "current": usable["system"]}
        if summary:
            best = summary[0]
            why = (f"best {best['playbook_id']} scored {best['score']:.2f}" if best["score"] < shared.RECALL_THRESHOLD
                   else f"{best['playbook_id']} was learned on {best['system']} v{best['system_version']}, "
                        f"now v{best['current_version']}")
            text = f"No usable playbook ({why}). Walking the chain from {inc['system']}."
        else:
            text = f"Memory is empty for these symptoms. Walking the chain from {inc['system']}."
        if lessons.enabled():  # visible staleness: say which memory a version bump retired
            note = lessons.stale_note(q)
            text = f"{text} {note}" if note else text
        say(inc_id, text, kind="recall", system=inc["system"])
        return {"hit": None, "diag_started": time.time(), "current": inc["system"]}

    def hop(config, state: ScoutState, sys: str, mode: str = "walk") -> tuple[dict, list[str]]:
        """One diagnosis hop = one diagnose task for a specialist worker on `sys`."""
        inc_id = state["incident"]
        inc = inc_doc(inc_id)
        path = list(state.get("path") or [])
        n = len(path) + 1
        # The recall confirm read gets its own id: if the remembered fault doesn't fit, the chain walk starts
        # again at hop 1 and must not pick up the confirm task's findings (another system's) as its own.
        task_id = f"{inc_id}-{'c' if mode == 'confirm' else 'd'}{n}"
        new_task(inc, task_id, "diagnose", policy.SPECIALTY[sys], system=sys, mode=mode)
        agent.set(target=sys, phase="diagnosing")
        if mode == "confirm":  # recall path: one quick confirmatory read, no deliberation pause
            say(inc_id, f"Confirm hop: one {policy.SPECIALTY[sys]} worker read of {sys}.", system=sys, pause=False)
        else:
            say(inc_id, f"Hop {n}: sending a {policy.SPECIALTY[sys]} worker to examine {sys}.", system=sys)
        t = wait_task(inc_id, task_id)
        path.append(sys)
        fenced(config, inc_id, {"$set": {"diagnosis_path": path, "metrics.hops": len(path)}})
        return (t.get("findings") or {"system": sys, "fault_class": None, "blames": None, "facts": []}), path

    def confirm(state: ScoutState, config) -> dict:
        """Recall path: one confirmatory read of the remembered root system instead of a full walk."""
        hit = state["hit"]
        f, path = hop(config, state, hit["system"], mode="confirm")
        findings = {**state.get("findings", {}), hit["system"]: f}
        if f.get("fault_class") == hit["fault_class"]:
            diag_s = round(time.time() - state["diag_started"], 2)
            say(state["incident"], f"{hit['system']} shows the remembered fault ({hit['fault_class']}); "
                                   f"applying {hit['playbook_id']}'s fix with today's facts.", kind="recall",
                system=hit["system"])
            return {"path": path, "findings": findings, "root": hit["system"], "fault_class": f["fault_class"],
                    "root_cause": f.get("root_cause"), "params": f.get("params") or {}, "diag_s": diag_s}
        inc = inc_doc(state["incident"])
        if lessons.enabled():  # failure memory: a wrong hypothesis is remembered, so the next recall skips it
            try:
                ls = lessons.record_misfit(state["incident"], hit, state["symptom_text"], f, agent.id,
                                           sysc.version(hit["system"]))
                say(state["incident"], f"Remembered {ls['_id']}: {ls['why']}.", kind="learn", system=hit["system"],
                    pause=False)
            except Exception:
                pass
        say(state["incident"], f"{hit['playbook_id']} doesn't fit what {hit['system']} shows; walking the chain "
                               f"from {inc['system']} instead.", kind="recall")
        return {"hit": None, "path": [], "findings": {}, "current": inc["system"]}

    def diagnose(state: ScoutState, config) -> dict:
        sys = state["current"]
        f, path = hop(config, state, sys)
        findings = {**state.get("findings", {}), sys: f}
        nxt = f.get("blames")
        if not nxt and not f.get("fault_class") and f.get("status") not in (None, "up"):
            # unhealthy, no fault of its own, blames nobody: its feed still comes from upstream
            nxt = shared.CHAIN.get(sys)
        if nxt and nxt in policy.systems() and nxt not in path:
            return {"path": path, "findings": findings, "current": nxt}
        return {"path": path, "findings": findings, "current": None}

    def classify(state: ScoutState, config) -> dict:
        inc_id = state["incident"]
        path, findings = state["path"], state["findings"]
        culprits = [s for s in path if (findings.get(s) or {}).get("fault_class")]
        if not culprits:
            return escalate(config, inc_id, f"walked {' → '.join(path)} but found no known fault")
        root = culprits[-1]  # most upstream system with a diagnosed fault
        f = findings[root]
        diag_s = round(time.time() - state["diag_started"], 2)
        say(inc_id, f"Root cause after {len(path)} hops ({' → '.join(path)}): {f['root_cause']} "
                    f"[{f['fault_class']} on {root}].", system=root, polish=True)
        return {"root": root, "fault_class": f["fault_class"], "root_cause": f.get("root_cause"),
                "params": f.get("params") or {}, "diag_s": diag_s}

    def plan(state: ScoutState, config) -> dict:
        inc_id = state["incident"]
        inc = inc_doc(inc_id)
        if not inc.get("steps"):
            if state.get("hit"):
                raw = policy.rebind(state["hit"]["fix"], state["fault_class"], state.get("params") or {})
            else:
                raw = policy.fix_plan(state["fault_class"], state.get("params") or {})
            if not raw:
                return escalate(config, inc_id, f"no fix plan for {state['fault_class']}")
            drove = None
            if not state.get("hit") and second_opinion.drives() and not second_opinion.owned_elsewhere(inc):
                # AI_DRIVES: Claude reasons over the evidence; its plan runs only if it passes every guardrail.
                # (A usable recall hit skips this: a fresh playbook already says what to do.)
                agent.check("incidents", inc_id)  # a scout that lost its lease must not flash a stale card
                drove = second_opinion.decide(agent.id, inc, state, raw, state["root"], state["fault_class"],
                                              say=lambda t: say(inc_id, t, system=state["root"], pause=False))
                raw = drove["plan"]
            plan_steps = [{
                "step_id": f"{inc_id}-s{i}", "n": i, "desc": s["desc"], "system": s["system"], "op": s["op"],
                "params": s["params"], "destructive": bool(s["destructive"] or (s["system"], s["op"]) in policy.DESTRUCTIVE),
                "task_id": None, "status": "planned", "action_id": None, "attempts": [], "agent": None,
                "evidence": None, "approval_id": None, "ts": now(),
                **{k: s[k] for k in ("source", "ai_why") if s.get(k)}} for i, s in enumerate(raw, 1)]
            fenced(config, inc_id, {"$set": {
                "steps": plan_steps, "status": "fixing", "root_cause_system": state["root"],
                "fault_class": state["fault_class"], "root_cause": state.get("root_cause"),
                "metrics.diagnosis_s": state.get("diag_s", 0.0), "metrics.hops": len(state.get("path") or [])}})
            # Claude's independent second opinion, judged against this plan in a background thread (never awaited)
            if drove is None and not state.get("hit") and not second_opinion.owned_elsewhere(inc):
                # (a recall hit skips it: keep Night 2's speedup beat clean)
                second_opinion.start(agent.id, inc, state, plan_steps, state["root"], state["fault_class"])
            desc = "; ".join(f"{s['op']}{' (needs approval)' if s['destructive'] else ''}" for s in plan_steps)
            who = "Claude's plan" if drove and drove["decision"] == "ai_plan" else "Plan"
            say(inc_id, f"{who}, {len(plan_steps)} steps: {desc}.", system=state["root"])
        return {}

    def next_group(st: list[dict]) -> list[dict]:
        """Next batch of steps one worker can take: same specialty, in order; a destructive step always
        starts its own batch so its approval is requested right before it runs."""
        pending = [s for s in sorted(st, key=lambda s: s["n"])
                   if s["status"] != "verified" and not s.get("superseded_by_replan")]
        if not pending:
            return []
        group = [pending[0]]
        for s in pending[1:]:
            if policy.SPECIALTY[s["system"]] != policy.SPECIALTY[group[0]["system"]] or s["destructive"]:
                break
            group.append(s)
        return group

    def approved(step: dict) -> bool:
        if not step.get("approval_id"):
            return False
        a = col("approvals").find_one({"_id": step["approval_id"]})
        return bool(a and a["status"] == "approved")

    def apply_lessons(config, inc: dict) -> dict:
        """Failure memory, once per incident right after the plan: annotate the planned steps with what failed
        before (stored on the incident, so a Nurse resuming this graph reads the same lessons) and say so."""
        if inc.get("lessons_checked") or not inc.get("steps"):
            return inc
        plan_steps = [dict(s) for s in inc["steps"]]
        try:
            _, lines = lessons.for_plan(plan_steps, sysc.version, inc["_id"])
        except Exception:
            lines = []
        sets = {"lessons_checked": now()}
        for i, s in enumerate(plan_steps):
            if s.get("lessons"):
                sets[f"steps.{i}.lessons"] = s["lessons"]
                if s.get("distrust_ack"):
                    sets[f"steps.{i}.distrust_ack"] = True
        fenced(config, inc["_id"], {"$set": sets})
        for line in lines:
            say(inc["_id"], line, kind="recall", system=inc.get("root_cause_system"), pause=False)
        return inc_doc(inc["_id"])

    def dispatch(state: ScoutState, config) -> dict:
        inc_id = state["incident"]
        inc = inc_doc(inc_id)
        if lessons.enabled():
            inc = apply_lessons(config, inc)
        group = next_group(inc.get("steps") or [])
        if not group:
            return {"outcome": "resolve"}
        first = group[0]
        if first.get("task_id"):
            t = col("tasks").find_one({"_id": first["task_id"]})
            if t and t["status"] in ("open", "claimed"):
                return {"outcome": "await"}
            if t and t["status"] == "failed":
                if recovery.enabled() and (inc.get("recall") or {}).get("rejected") and not inc.get("memory_replanned"):
                    return replan_memory(state, config, inc)
                if recovery.enabled() and inc.get("recovery_rounds", 0) < recovery.ROUNDS:
                    return recover(config, inc, t)
                return escalate(config, inc_id, f"{first['step_id']} ({first['op']}) could not be completed"
                                + (f" after {recovery.ROUNDS} recovery rounds" if recovery.enabled() else ""))
        if first["destructive"] and not approved(first):
            return {"outcome": "approve"}
        task_id = f"{inc_id}-f{first['n']}"
        ids = [s["step_id"] for s in group]
        new_task(inc, task_id, "fix", policy.SPECIALTY[first["system"]], step_ids=ids, system=first["system"])
        fenced(config, inc_id, {"$set": {"steps.$[g].task_id": task_id, "status": "fixing"}},
               array_filters=[{"g.step_id": {"$in": ids}}])
        agent.set(target=first["system"], phase="fixing")
        say(inc_id, f"Dispatching {', '.join(s['op'] for s in group)} to a {policy.SPECIALTY[first['system']]} "
                    f"worker ({task_id}).", kind="action", system=first["system"])
        return {"outcome": "await"}

    def replan_memory(state, config, inc):
        """Keep the rejected attempts in the audit trail; replace their plan with live-policy steps."""
        raw = policy.fix_plan(state["fault_class"], state.get("params") or {})
        if not raw:
            return escalate(config, inc["_id"], "rejected memory has no safe policy replacement")
        old = inc["steps"]
        done = [(s["system"], s["op"], s.get("params") or {}) for s in old if s["status"] == "verified"]
        n = max(s["n"] for s in old)
        new = []
        for s in raw:
            if (s["system"], s["op"], s.get("params") or {}) in done:
                continue
            n += 1
            new.append({**s, "step_id": f"{inc['_id']}-s{n}", "n": n, "task_id": None,
                        "status": "planned", "action_id": None, "attempts": [], "agent": None,
                        "evidence": None, "approval_id": None, "ts": now()})
        # One server-side pipeline update: flags whatever is unverified *at write time* and appends the new
        # steps, so a Guard verdict that lands between our read and this write is never overwritten.
        fenced(config, inc["_id"], [{"$set": {
            "steps": {"$concatArrays": [
                {"$map": {"input": "$steps", "as": "s", "in": {"$cond": [
                    {"$in": ["$$s.status", ["planned", "failed"]]},
                    {"$mergeObjects": ["$$s", {"superseded_by_replan": True}]}, "$$s"]}}},
                {"$literal": new}]},
            "memory_replanned": True, "status": "fixing"}}])
        say(inc["_id"], "The Guard rejected the recalled repair. Replacing unverified work with a fresh policy plan; "
                        "keeping the failed attempts as evidence.", kind="action", pause=False)
        return {"outcome": "await"}

    def recover(config, inc: dict, failed_task: dict) -> dict:
        """SWARM_RECOVERY: the fix task ran out of attempts (or timed out). Back off, re-read the system,
        and re-dispatch the unverified steps with a fresh attempt budget instead of giving up."""
        inc_id = inc["_id"]
        rnd = inc.get("recovery_rounds", 0) + 1
        ids = set(failed_task.get("step_ids") or [])
        todo = [s for s in sorted(inc.get("steps") or [], key=lambda s: s["n"])
                if s["step_id"] in ids and s["status"] != "verified"]
        if not todo:  # nothing left to retry: the next dispatch pass moves on
            return {"outcome": "await"}
        first = todo[0]
        sys = first["system"]
        used = sum(len(s.get("attempts") or []) for s in todo)
        agent.set(target=sys, phase="recovering")
        say(inc_id, f"Recovery {rnd}/{recovery.ROUNDS}: {first['step_id']} {policy.fmt_op(first)} did not take effect "
                    f"after {used} attempt(s): {recovery.why_failed(todo)}. Not giving up: backing off "
                    f"{recovery.BACKOFF_S * rnd:.0f}s, re-reading {sys}, then a fresh attempt budget.",
            kind="action", system=sys, pause=False)
        agent.nap(recovery.BACKOFF_S * rnd)
        agent.check("incidents", inc_id)
        try:
            h = sysc.health(sys)
            say(inc_id, f"Re-read {sys}: /health {h.get('status')}"
                        + (f", blames {h['blames']}" if h.get("blames") else "") + ". Re-dispatching "
                        f"{', '.join(s['op'] for s in todo)} with {settings.MAX_ATTEMPTS} more attempts each.",
                kind="action", system=sys, pause=False)
        except Exception:
            pass
        task_id = f"{inc_id}-f{first['n']}r{rnd}"
        # task first, then the incident write: a resume after a crash in between re-creates the same id (no-op)
        new_task(inc, task_id, "fix", policy.SPECIALTY[sys], step_ids=[s["step_id"] for s in todo], system=sys)
        sets = {"recovery_rounds": rnd, "status": "fixing"}
        filters = []
        for i, s in enumerate(todo):
            sets[f"steps.$[r{i}].task_id"] = task_id
            sets[f"steps.$[r{i}].max_attempts"] = len(s.get("attempts") or []) + settings.MAX_ATTEMPTS
            filters.append({f"r{i}.step_id": s["step_id"]})
        fenced(config, inc_id, {"$set": sets, "$push": {"recoveries": {
            "round": rnd, "step_ids": [s["step_id"] for s in todo], "attempts_used": used, "task": task_id,
            "reason": recovery.why_failed(todo), "ts": now()}}}, array_filters=filters)
        return {"outcome": "await"}

    def approve(state: ScoutState, config) -> dict:
        inc_id = state["incident"]
        inc = inc_doc(inc_id)
        step = next_group(inc["steps"])[0]
        apr = col("approvals").find_one({"incident": inc_id, "step_id": step["step_id"], "kind": "step",
                                         "status": {"$in": ["pending", "approved"]}})
        if apr is None:  # idempotent: this node re-runs when resumed after the interrupt
            apr = {"_id": next_id("APR"), "kind": "step", "incident": inc_id, "step_id": step["step_id"],
                   "action_id": steps.attempt_id(step["step_id"], len(step.get("attempts") or []) + 1),
                   "upgrade": None, "requested_by": agent.id, "relayed_by": None,
                   "desc": f"{policy.fmt_op(step)} on {step['system']}: {step['desc']}",
                   "risk": policy.RISK.get(step["op"], "Destructive operation on a production system."),
                   "status": "pending", "decided_by": None, "ts": now(), "decided_at": None}
            col("approvals").insert_one(apr)
            agent.set(phase="awaiting_approval", target="hive")
            say(inc_id, f"{step['op']} on {step['system']} is destructive — asking the Beekeeper ({apr['_id']}).",
                kind="approval", system=step["system"])
        if step.get("approval_id") != apr["_id"]:
            # Link (or repair the link, if we crashed between the insert above and this write):
            # dispatch only treats the step as approved through steps[].approval_id.
            fenced(config, inc_id, {"$set": {"steps.$[s].approval_id": apr["_id"], "status": "awaiting_approval"}},
                   array_filters=[{"s.step_id": step["step_id"]}])
        decision = interrupt({"approval_id": apr["_id"], "step_id": step["step_id"], "op": step["op"]})
        if decision.get("status") == "approved":
            fenced(config, inc_id, {"$set": {"status": "fixing"}})
            say(inc_id, f"Beekeeper approved {apr['_id']} ({decision.get('decided_by') or 'beekeeper'}); proceeding.",
                kind="approval", system=step["system"])
            return {"outcome": "approved"}
        fenced(config, inc_id, {"$set": {"steps.$[s].status": "failed",
                                          "steps.$[s].evidence": f"approval {apr['_id']} denied"}},
               array_filters=[{"s.step_id": step["step_id"]}])
        return escalate(config, inc_id, f"Beekeeper denied {apr['_id']} ({step['op']} on {step['system']})")

    def await_group(state: ScoutState, config) -> dict:
        inc = inc_doc(state["incident"])
        group = next_group(inc.get("steps") or [])
        if group and group[0].get("task_id"):
            wait_task(state["incident"], group[0]["task_id"])
        return {}

    def resolve(state: ScoutState, config) -> dict:
        inc_id = state["incident"]
        inc = inc_doc(inc_id)
        fenced(config, inc_id, {"$set": {"status": "verifying"}})
        agent.set(phase="verifying", target=state["root"])
        watch = sorted(set(inc.get("affected") or []) | {state["root"]}, key=policy.rank)
        verified = "Current plan" if inc.get("memory_replanned") else "Every step"
        say(inc_id, f"{verified} verified by the Guard. Waiting for {', '.join(watch)} to report healthy.")
        deadline, health = time.monotonic() + settings.RESOLVE_TIMEOUT_S, {}
        while time.monotonic() < deadline:
            agent.check("incidents", inc_id)
            health = {s: sysc.health(s).get("status") for s in watch}
            if all(v == "up" for v in health.values()):
                break
            agent.nap(settings.POLL_S)
        else:
            return escalate(config, inc_id, f"steps verified but health still {health}")
        inc = inc_doc(inc_id)
        closed = now()
        approvals = list(col("approvals").find({"incident": inc_id, "decided_at": {"$ne": None}}))
        metrics = {
            "ttr_s": round((closed - inc["opened_at"]).total_seconds(), 2),
            "diagnosis_s": state.get("diag_s", 0.0),
            "approval_wait_s": round(sum((a["decided_at"] - a["ts"]).total_seconds() for a in approvals), 2),
            "takeover_s": round(sum(h.get("takeover_s") or 0 for h in inc.get("handoffs") or []), 2),
            "hops": len(inc.get("diagnosis_path") or []),
            "steps": len(inc.get("steps") or []),
            "attempts": sum(len(s.get("attempts") or []) for s in inc.get("steps") or []),
        }
        resolved = {**inc, "metrics": metrics, "root_cause_system": state["root"], "fault_class": state["fault_class"]}
        pb = recall.build_playbook(resolved, state["symptom_text"], sysc.version(state["root"]))
        me, gen = _who(config)

        def fn(s):  # resolve + learn atomically
            leases.fence(col("incidents"), inc_id, me, gen, s)
            col("incidents").update_one({"_id": inc_id}, {"$set": {
                "status": "resolved", "active": False, "closed_at": closed, "metrics": metrics,
                "playbook_id": pb["_id"], "lease_until": leases.EPOCH}}, session=s)
            col("playbooks").insert_one(pb, session=s)
            if state.get("hit"):
                col("playbooks").update_one({"_id": state["hit"]["playbook_id"]},
                                            {"$inc": {"uses": 1}, "$set": {"last_used_at": closed}}, session=s)
        txn(fn)
        say(inc_id, f"Resolved {inc_id} in {metrics['ttr_s']:.0f}s ({metrics['hops']} hops, {metrics['steps']} steps, "
                    f"{metrics['attempts']} attempts). Saved playbook {pb['_id']} for {state['root']} "
                    f"v{pb['system_version']}.", kind="learn", system=state["root"], pause=False)
        return {"outcome": "resolved"}

    g = StateGraph(ScoutState)
    for name, fn in [("observe", observe), ("recall", recall_node), ("confirm", confirm), ("diagnose", diagnose),
                     ("classify", classify), ("plan", plan), ("dispatch", dispatch), ("approve", approve),
                     ("await_group", await_group), ("resolve", resolve)]:
        g.add_node(name, fn)
    failed = lambda s: s.get("outcome") == "failed"  # noqa: E731
    g.add_edge(START, "observe")
    g.add_edge("observe", "recall")
    g.add_conditional_edges("recall", lambda s: "confirm" if s.get("hit") else "diagnose", ["confirm", "diagnose"])
    g.add_conditional_edges("confirm", lambda s: "plan" if s.get("root") and s.get("hit") else "diagnose",
                            ["plan", "diagnose"])
    g.add_conditional_edges("diagnose", lambda s: "diagnose" if s.get("current") else "classify",
                            ["diagnose", "classify"])
    g.add_conditional_edges("classify", lambda s: END if failed(s) else "plan", ["plan", END])
    g.add_conditional_edges("plan", lambda s: END if failed(s) else "dispatch", ["dispatch", END])
    g.add_conditional_edges("dispatch", lambda s: {"resolve": "resolve", "await": "await_group",
                                                   "approve": "approve"}.get(s.get("outcome"), END),
                            ["resolve", "await_group", "approve", END])
    g.add_conditional_edges("approve", lambda s: END if failed(s) else "dispatch", ["dispatch", END])
    g.add_edge("await_group", "dispatch")
    g.add_edge("resolve", END)
    return g.compile(checkpointer=checkpointer())


def drive(agent: Agent, graph, inc_id: str, generation: int) -> dict | None:
    """Run (or resume) the scout graph for an incident, servicing approval interrupts by polling
    swarm.approvals. Shared by scouts and by nurses that take over a dead scout's incident."""
    cfg = scout_config(inc_id, agent.id, generation)
    snap = graph.get_state(cfg)
    if snap.values and not snap.next:
        return snap.values
    inp = None if snap.values else {"incident": inc_id}
    while True:
        result = graph.invoke(inp, cfg, durability="sync")
        pending = result.get("__interrupt__") if isinstance(result, dict) else None
        if not pending:
            return result
        apr_id = pending[0].value["approval_id"]
        while True:
            agent.check("incidents", inc_id)
            a = col("approvals").find_one({"_id": apr_id})
            if a and a["status"] != "pending":
                break
            agent.nap(settings.POLL_S)
        inp = Command(resume={"status": a["status"], "decided_by": a.get("decided_by")})


def run(agent: Agent) -> None:
    graph = build_graph(agent)
    second_opinion.prewarm()  # no-op unless an AI switch is on and a key is set
    def tick():
        # Take an incident the queen assigned to me — or any unassigned one if the queen is slow/absent.
        inc = leases.claim(col("incidents"), {
            "active": True, "status": "open",
            "$and": [{"$or": [{"assigned_to": agent.id},
                              {"assigned_to": None, "opened_at": {"$lt": now() - timedelta(seconds=2)}}]}],
        }, agent.id, extra_set={"status": "claimed", "claimed_at": now()}, sort=[("opened_at", 1)])
        if not inc:
            return
        agent.hold("incidents", inc["_id"], inc["generation"])
        try:
            agent.set(status="working", incident=inc["_id"], target=inc["system"], phase="claimed")
            agent.think(f"Claimed {inc['_id']} (lease {shared.LEASE_S:.0f}s, generation {inc['generation']}).",
                        kind="action", incident=inc["_id"], system=inc["system"])
            retrying(agent, lambda: drive(agent, graph, inc["_id"], inc["generation"]), inc["_id"])
        finally:
            agent.drop("incidents", inc["_id"])
            agent.rest()

    main_loop(agent, tick, settings.POLL_S)
