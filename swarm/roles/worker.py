"""Worker bee (specialty mainframe | unix | web).

Claims tasks for its specialty with an atomic lease, then runs the task as a LangGraph graph whose
checkpoint thread is the TASK id — so if this process is SIGKILLed, a Nurse loads the same thread and
continues exactly where it stopped.

    diagnose task:  investigate -> report
    fix task:       select -> act -> await_verify -> select ... -> finish

Every step write is a transaction fenced on the task's {owner, generation} (see swarm.steps).
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from common.db import col, now, txn

from .. import diagnosis, leases, recovery, schema, settings, steps, verify
from ..policy import fmt_op
from ..harness import Agent, checkpointer, main_loop, retrying
from ..http import SystemsUnavailable
from .. import lessons
from gardener import help as gardener_help


LOG_WINDOW_S = 300  # only logs from shortly before the incident opened count as clues


class WorkerState(TypedDict, total=False):
    task_id: str
    incident: str
    kind: str
    step: str | None
    findings: dict
    outcome: str


def _who(config) -> tuple[str, int]:
    c = config["configurable"]
    return c["agent"], int(c["generation"])


def narrate_findings(f: dict) -> str:
    sys = f["system"].capitalize()
    status = f.get("status") or "unknown"
    lead = f"{sys} /health says {status}"
    facts = "; ".join(f.get("facts") or []) or "nothing abnormal in logs or state"
    tail = f" {sys} blames {f['blames']} — walking upstream." if f.get("blames") else ""
    if f.get("fault_class") and not f.get("blames"):
        tail = f" Root cause here: {f['root_cause']}."
    return f"{lead}: {facts}.{tail}"


def build_graph(agent: Agent):
    sysc = agent.systems

    def task_doc(tid: str) -> dict:
        return col("tasks").find_one({"_id": tid})

    # ------------------------------------------------------------------ diagnose
    def investigate(state: WorkerState, config) -> dict:
        task = task_doc(state["task_id"])
        sys, inc_id = task["system"], task["incident"]
        agent.check("tasks", task["_id"])
        agent.set(status="working", target=sys, phase="diagnosing", incident=inc_id, task=task["_id"])
        inc = col("incidents").find_one({"_id": inc_id}, {"opened_at": 1}) or {}
        since = (inc.get("opened_at") or now()) - timedelta(seconds=LOG_WINDOW_S)
        if task.get("mode") == "confirm":  # recall path: one quick confirmatory read
            agent.think(f"Confirming the remembered fault on {sys}: one read of its logs and state.",
                        incident=inc_id, system=sys)
            h, m = sysc.health(sys), sysc.metrics(sys)
        else:  # chain walk: read like an engineer would, one source at a time
            agent.think(f"Reading {sys} /health and /metrics.", incident=inc_id, system=sys)
            h, m = sysc.health(sys), sysc.metrics(sys)
            agent.think(f"{sys.capitalize()} reports {h.get('status')}"
                        + (f" and blames {h['blames']}" if h.get("blames") else ", blaming nothing upstream")
                        + ". Scanning its logs and state for clues.", incident=inc_id, system=sys)
        logs, st = sysc.logs(sys, limit=50, since=since.isoformat()), sysc.state(sys)
        f = diagnosis.classify(sys, h, m, logs, st, probe=sysc.probe)
        ai = (f.get("params") or {}).pop("ai", None)
        if ai:  # schema drift: who proposed the mapping (advisory, shared incident.ai shape)
            try:
                schema.record_ai(inc_id, ai)
            except Exception:
                pass
        agent.think(narrate_findings(f), incident=inc_id, system=sys, pause=task.get("mode") != "confirm")
        return {"findings": f}

    def report(state: WorkerState, config) -> dict:
        me, gen = _who(config)
        tid = state["task_id"]

        def fn(s):
            leases.fence(col("tasks"), tid, me, gen, s)
            col("tasks").update_one({"_id": tid}, {"$set": {"findings": state["findings"], "status": "done",
                                                           "done_at": now(), "lease_until": leases.EPOCH}}, session=s)
        txn(fn)
        return {"outcome": "done"}

    # ------------------------------------------------------------------ fix
    def select(state: WorkerState, config) -> dict:
        task = task_doc(state["task_id"])
        inc = col("incidents").find_one({"_id": task["incident"]}, {"steps": 1})
        mine = sorted((s for s in inc.get("steps", []) if s["step_id"] in task["step_ids"]), key=lambda s: s["n"])
        for s in mine:
            if s["status"] == "verified":
                continue
            if s["status"] == "failed" and len(s.get("attempts") or []) >= recovery.budget(s):
                agent.think(f"{s['step_id']} failed {len(s.get('attempts') or [])} attempts; giving the task back to the scout.",
                            incident=task["incident"], system=s["system"], pause=False)
                return {"step": None, "outcome": "failed"}
            return {"step": s["step_id"], "outcome": "working"}
        return {"step": None, "outcome": "done"}

    def act(state: WorkerState, config) -> dict:
        me, gen = _who(config)
        task = task_doc(state["task_id"])
        agent.check("tasks", task["_id"])
        step = steps.get_step(task["incident"], state["step"])
        if step["status"] not in ("planned", "failed"):
            # Resumed mid-step: the attempt is already 'sent'. If nobody recorded the system's answer
            # (this process failed between mark_sent and the POST), reconcile by read-back first.
            agent.set(status="working", target=step["system"], phase="awaiting_verify",
                      incident=task["incident"], task=task["_id"])
            if step["status"] == "sent" and step.get("response") is None:
                reconcile_sent(agent, task, gen, step)
            return {}
        n = len(step.get("attempts") or []) + 1
        if (n > 1 and step["status"] == "failed" and step.get("action_id") and recovery.enabled()
                and step.get("resubmitted") != step["action_id"]):  # once per attempt: never a resubmit loop
            # Reconcile before retry: the last attempt may have landed late (slow_apply) after the Guard gave up.
            # Then the effect is already there: hand that attempt back to the Guard instead of sending again.
            applied, facts = verify.check(step["op"], step.get("params") or {}, sysc.state, probe=sysc.probe,
                                          sent_at=step.get("sent_at"))
            if applied and steps.resubmit(task, me, gen, step):
                agent.set(status="working", target=step["system"], phase="awaiting_verify",
                          incident=task["incident"], task=task["_id"])
                agent.think(f"Before retrying: {step['system']} already shows {step['op']} took effect ({facts}); "
                            f"{step['action_id']} landed late. Handing it back to the Guard, not sending it again.",
                            kind="action", incident=task["incident"], system=step["system"], pause=False)
                return {}
        aid = steps.attempt_id(step["step_id"], n)
        advice = {}
        if n > 2 and gardener_help.enabled():  # failed verification twice: ask the Gardener (GARDENER_HELP=1)
            advice = ask_gardener(agent, task, step)
            if advice.get("params"):
                step = steps.get_step(task["incident"], state["step"])
        # failure memory: this system lied about this op before -> the retry goes straight out
        fast = n > 1 and ((bool(step.get("distrust_ack")) and lessons.enabled()) or bool(advice.get("fast")))
        if n > 1:
            agent.think(f"Guard rejected {step['action_id']}; opening new attempt {aid}"
                        + ((" at once (the Gardener's advice)." if advice.get("fast") else " at once (a known lie, see the lesson).")
                       if fast else "."), kind="action",
                        incident=task["incident"], system=step["system"], pause=not fast)
        approver = None
        if step.get("approval_id"):
            apr = col("approvals").find_one({"_id": step["approval_id"]}) or {}
            approver = apr.get("decided_by")
        steps.mark_sent(task, me, gen, step, aid, approver)
        agent.set(status="working", target=step["system"], phase="step_sent", incident=task["incident"],
                  task=task["_id"])
        agent.think(f"Sending {fmt_op(step)} to {step['system']} as {aid}.",
                    kind="action", incident=task["incident"], system=step["system"], pause=False)
        agent.nap(0 if fast else settings.step_hold_s())
        agent.check("tasks", task["_id"])
        try:
            resp = sysc.post_op(step["system"], step["op"], aid, step.get("params") or {})
        except SystemsUnavailable as e:
            resp = {"accepted": None, "applied": None, "error": str(e), "action_id": aid}
        steps.record_response(task, me, gen, step, aid, resp)
        agent.set(phase="awaiting_verify")
        agent.think(f"{step['system']} answered accepted={resp.get('accepted')} applied={resp.get('applied')} "
                    f"for {aid}; waiting for the Guard to read it back.", kind="action",
                    incident=task["incident"], system=step["system"])
        return {}

    def await_verify(state: WorkerState, config) -> dict:
        task = task_doc(state["task_id"])
        deadline = time.monotonic() + settings.TASK_TIMEOUT_S
        while time.monotonic() < deadline:
            agent.check("tasks", task["_id"])
            step = steps.get_step(task["incident"], state["step"])
            if step["status"] != "sent":
                return {}
            agent.nap(settings.POLL_S)
        return {"outcome": "failed"}

    def finish(state: WorkerState, config) -> dict:
        me, gen = _who(config)
        status = "failed" if state.get("outcome") == "failed" else "done"

        def fn(s):
            leases.fence(col("tasks"), state["task_id"], me, gen, s)
            col("tasks").update_one({"_id": state["task_id"]},
                                    {"$set": {"status": status, "done_at": now(), "lease_until": leases.EPOCH}}, session=s)
        txn(fn)
        return {"outcome": status}

    g = StateGraph(WorkerState)
    for name, fn in [("investigate", investigate), ("report", report), ("select", select), ("act", act),
                     ("await_verify", await_verify), ("finish", finish)]:
        g.add_node(name, fn)
    g.add_conditional_edges(START, lambda s: "investigate" if s["kind"] == "diagnose" else "select",
                            ["investigate", "select"])
    g.add_edge("investigate", "report")
    g.add_edge("report", END)
    g.add_conditional_edges("select", lambda s: "act" if s.get("step") else "finish", ["act", "finish"])
    g.add_edge("act", "await_verify")
    g.add_conditional_edges("await_verify", lambda s: "finish" if s.get("outcome") == "failed" else "select",
                            ["select", "finish"])
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer())


def ask_gardener(agent: Agent, task: dict, step: dict) -> dict:
    """A step failed verification twice: post a help request with the evidence, wait briefly for the
    Gardener's answer and return its advice ({} if none). Advice params are written onto the step so the
    next attempt (and the Guard's read-back of it) uses them. Idempotent: a resumed act reuses the answer."""
    inc, sid = task["incident"], step["step_id"]
    attempts = list(step.get("attempts") or [])
    ledger = list(col("ledger").find({"action_id": {"$in": attempts}}, {"action_id": 1, "evidence": 1, "status": 1})
                  .sort("ts", 1))
    evidence = [f"{d['action_id']} {d.get('status')}: {d.get('evidence') or ''}"[:200] for d in ledger]
    fc = (col("incidents").find_one({"_id": inc}, {"fault_class": 1}) or {}).get("fault_class")
    req, new = gardener_help.ask(agent.id, inc, step, evidence, fc)
    if new:
        last = (ledger[-1].get("evidence") if ledger else None) or step.get("evidence") or "rejected"
        agent.think(f"{sid} failed verification {len(attempts)}× ({str(last)[:90]}); asking the Gardener for help "
                    f"({req['_id']}).", kind="handoff", incident=inc, system=step["system"], pause=False)
    ans = req.get("answer") if req.get("status") == "answered" else gardener_help.await_answer(
        req["_id"], gardener_help.wait_s(), nap=agent.nap, check=lambda: agent.check("tasks", task["_id"]))
    if not ans:
        if new:
            agent.think(f"No answer from the Gardener in {gardener_help.wait_s():.0f}s; retrying {sid} as planned.",
                        incident=inc, system=step["system"], pause=False)
        return {}
    advice = dict(ans.get("advice") or {})
    use = []
    if advice.get("params"):
        col("incidents").update_one(
            {"_id": inc}, {"$set": {"steps.$[s].params": advice["params"],
                                    "steps.$[s].params_before_help": step.get("params") or {}}},
            array_filters=[{"s.step_id": sid, "s.status": "failed"}])
        use.append(f"next attempt uses params {advice['params']}")
    if advice.get("fast"):
        use.append("next attempt goes out at once")
    col("incidents").update_one({"_id": inc}, {"$set": {"steps.$[s].gardener_help": {
        "request": req["_id"], "kind": ans.get("kind"), "ref": ans.get("ref"), "advice": advice}}},
        array_filters=[{"s.step_id": sid}])
    if new:
        agent.think(f"Gardener's answer ({ans.get('kind')}) for {sid}: "
                    + ("; ".join(use) if use else "nothing to change, retrying as planned")
                    + "; the Guard still verifies it by read-back.", kind="action", incident=inc,
                    system=step["system"], pause=False)
    return advice


def reconcile_sent(agent: Agent, task: dict, generation: int, st: dict, adopt: bool = False) -> dict:
    """A step is 'sent' but this process never saw the system's answer (it crashed, or a nurse is
    adopting a dead worker's step). Never resend blindly: READ BACK /state first.
        already applied -> leave it for the Guard (no resend)
        not applied     -> resend with the SAME action_id (the system de-duplicates receipts)
    Returns {step_id, action_id, result[, response]}."""
    sysc = agent.systems
    agent.check("tasks", task["_id"])
    applied, facts = verify.check(st["op"], st.get("params") or {}, sysc.state, probe=sysc.probe,
                                  sent_at=st.get("sent_at"))
    if adopt:
        steps.take_over_sent(task, agent.id, generation, st)
    if applied:
        agent.think(f"{st['action_id']} was sent before the crash and already took effect ({facts}) — "
                    f"handing to the Guard, not resending.", kind="handoff", incident=task["incident"],
                    system=st["system"])
        return {"step_id": st["step_id"], "action_id": st["action_id"], "result": "already_applied"}
    agent.think(f"{st['action_id']} is 'sent' but not applied ({facts}) — resending the SAME action id; "
                f"the system de-duplicates.", kind="handoff", incident=task["incident"], system=st["system"])
    agent.set(target=st["system"], phase="step_sent")
    try:
        resp = sysc.post_op(st["system"], st["op"], st["action_id"], st.get("params") or {})
    except SystemsUnavailable as e:
        resp = {"accepted": None, "applied": None, "error": str(e), "action_id": st["action_id"]}
    steps.record_response(task, agent.id, generation, st, st["action_id"], resp)
    agent.set(phase="awaiting_verify")
    return {"step_id": st["step_id"], "action_id": st["action_id"], "result": "resent_same_action_id",
            "response": resp}


def task_config(task_id: str, agent_id: str, generation: int) -> dict:
    return {"configurable": {"thread_id": task_id, "agent": agent_id, "generation": generation},
            "recursion_limit": 500}


def execute(agent: Agent, graph, task: dict, resume: bool = False) -> None:
    """Run (or resume) a task's graph while holding its lease. Thread id = task id."""
    gen = int(task["generation"])
    agent.hold("tasks", task["_id"], gen)
    try:
        cfg = task_config(task["_id"], agent.id, gen)
        inp = {"task_id": task["_id"], "incident": task["incident"], "kind": task["kind"]}
        if resume:
            snap = graph.get_state(cfg)
            if snap.values and not snap.next:  # graph had finished; only the bookkeeping was lost
                col("tasks").update_one({"_id": task["_id"], "owner": agent.id, "generation": gen},
                                        {"$set": {"status": snap.values.get("outcome", "done"),
                                                  "lease_until": leases.EPOCH}})
                return
        def go():  # retries continue from the last checkpoint (if the first run got that far)
            graph.invoke(inp if not graph.get_state(cfg).values else None, cfg, durability="sync")
        retrying(agent, go, task["_id"])
    finally:
        agent.drop("tasks", task["_id"])
        agent.rest()


def run(agent: Agent) -> None:
    graph = build_graph(agent)

    def tick():
        task = leases.claim(col("tasks"), {"status": "open", "specialty": agent.specialty}, agent.id,
                            extra_set={"status": "claimed", "claimed_at": now()}, sort=[("ts", 1)])
        if task:
            execute(agent, graph, task)

    main_loop(agent, tick, settings.POLL_S)
