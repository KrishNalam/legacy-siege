"""Step transitions. Each one is ONE multi-document transaction:
    (fence the task lease) + (update the incident's step) + (upsert that attempt's ledger receipt)
so the incident timeline and the audit ledger can never disagree, and a replaced operator can't write.
"""
from __future__ import annotations

from pymongo.client_session import ClientSession

from common import progress
from common.db import col, now, txn

from . import leases, lessons


class StepConflict(RuntimeError):
    """The step was not in the state this transition expects (someone else moved it)."""


def attempt_id(step_id: str, n: int) -> str:
    return f"{step_id}-a{n}"


def get_step(incident: str, step_id: str) -> dict | None:
    inc = col("incidents").find_one({"_id": incident, "steps.step_id": step_id}, {"steps.$": 1})
    return inc["steps"][0] if inc else None


def _ledger_upsert(s: ClientSession, step: dict, incident: str, action_id: str, agent: str, status: str,
                   extra: dict | None = None) -> None:
    col("ledger").update_one(
        {"action_id": action_id},
        {"$setOnInsert": {"action_id": action_id, "step_id": step["step_id"], "incident": incident,
                          "task_id": step.get("task_id"), "system": step["system"], "op": step["op"],
                          "params": step.get("params", {}), "ts": now()},
         "$set": {"agent": agent, "status": status, "updated_at": now(), **(extra or {})}},
        upsert=True, session=s)


def mark_sent(task: dict, agent: str, generation: int, step: dict, action_id: str, approver: str | None = None) -> None:
    """planned|failed -> sent (new attempt), fenced on the task's {owner, generation}."""
    def fn(s: ClientSession):
        leases.fence(col("tasks"), task["_id"], agent, generation, s)
        r = col("incidents").update_one(
            {"_id": task["incident"], "steps": {"$elemMatch": {"step_id": step["step_id"],
                                                               "status": {"$in": ["planned", "failed"]}}}},
            {"$set": {"steps.$[s].status": "sent", "steps.$[s].action_id": action_id, "steps.$[s].agent": agent,
                      "steps.$[s].sent_at": now(), "steps.$[s].response": None, "steps.$[s].evidence": None,
                      "steps.$[s].ts": now()},
             "$addToSet": {"steps.$[s].attempts": action_id}},
            array_filters=[{"s.step_id": step["step_id"]}], session=s)
        if r.matched_count != 1:
            raise StepConflict(f"{step['step_id']} not planned/failed")
        _ledger_upsert(s, step, task["incident"], action_id, agent, "sent", {"approver": approver})
    txn(fn)


def resubmit(task: dict, agent: str, generation: int, step: dict) -> bool:
    """failed -> sent for the SAME attempt (its effect landed after the Verifier rejected it), so the Verifier
    re-judges it by read-back. No new action_id, nothing re-sent to the system. False if the step moved."""
    def fn(s: ClientSession):
        leases.fence(col("tasks"), task["_id"], agent, generation, s)
        r = col("incidents").update_one(
            {"_id": task["incident"], "steps": {"$elemMatch": {"step_id": step["step_id"], "status": "failed",
                                                               "action_id": step["action_id"]}}},
            {"$set": {"steps.$[s].status": "sent", "steps.$[s].agent": agent, "steps.$[s].evidence": None,
                      "steps.$[s].resubmitted": step["action_id"], "steps.$[s].ts": now()}},
            array_filters=[{"s.step_id": step["step_id"]}], session=s)
        if r.matched_count != 1:
            return False
        col("ledger").update_one({"action_id": step["action_id"]},
                                 {"$set": {"status": "sent", "agent": agent, "resubmitted": True,
                                           "updated_at": now()}}, session=s)
        return True
    return txn(fn)


def take_over_sent(task: dict, agent: str, generation: int, step: dict) -> None:
    """A recovery adopts a step stuck in 'sent' (same attempt, same action_id): record the new agent."""
    def fn(s: ClientSession):
        leases.fence(col("tasks"), task["_id"], agent, generation, s)
        col("incidents").update_one(
            {"_id": task["incident"], "steps": {"$elemMatch": {"step_id": step["step_id"], "action_id": step["action_id"]}}},
            {"$set": {"steps.$[s].agent": agent, "steps.$[s].ts": now()}},
            array_filters=[{"s.step_id": step["step_id"]}], session=s)
        col("ledger").update_one({"action_id": step["action_id"]}, {"$set": {"agent": agent, "updated_at": now()}},
                                 session=s)
    txn(fn)


def record_response(task: dict, agent: str, generation: int, step: dict, action_id: str, response: dict) -> None:
    def fn(s: ClientSession):
        leases.fence(col("tasks"), task["_id"], agent, generation, s)
        col("incidents").update_one(
            {"_id": task["incident"], "steps": {"$elemMatch": {"step_id": step["step_id"], "action_id": action_id}}},
            {"$set": {"steps.$[s].response": response, "steps.$[s].responded_at": now()}},
            array_filters=[{"s.step_id": step["step_id"]}], session=s)
        col("ledger").update_one({"action_id": action_id},
                                 {"$set": {"system_response": response, "updated_at": now()}}, session=s)
    txn(fn)


def _judge(incident: str, step: dict, action_id: str, status: str, evidence: str, agent: str) -> bool:
    """Verifier-only: sent -> verified|failed for exactly this attempt. Returns False if the step moved."""
    def fn(s: ClientSession):
        r = col("incidents").update_one(
            {"_id": incident, "steps": {"$elemMatch": {"step_id": step["step_id"], "action_id": action_id,
                                                       "status": "sent"}}},
            {"$set": {"steps.$[s].status": status, "steps.$[s].evidence": evidence,
                      "steps.$[s].judged_by": agent, "steps.$[s].judged_at": now(), "steps.$[s].ts": now()}},
            array_filters=[{"s.step_id": step["step_id"]}], session=s)
        if r.matched_count != 1:
            return False
        _ledger_upsert(s, step, incident, action_id, step.get("agent") or agent, status, {"evidence": evidence,
                                                                                          "judged_by": agent})
        return True
    return txn(fn)


def _judged(incident: str, step: dict, action_id: str, status: str, agent: str) -> None:
    """PROGRESS_ETA: one duration record per judged attempt (sent -> verified|failed) + i/n verified line."""
    if not progress.enabled():
        return
    try:
        inc = col("incidents").find_one({"_id": incident}, {"steps": 1}) or {}
        all_steps = inc.get("steps") or []
        cur = next((x for x in all_steps if x.get("step_id") == step["step_id"]), step)
        sent = cur.get("sent_at") or step.get("sent_at")
        took = (now() - sent).total_seconds() if sent else None
        if took is not None:
            progress.record("swarm", f"step:{cur['op']}", cur["system"], "operator", took, status,
                            incident=incident, action_id=action_id)
        done = sum(1 for x in all_steps if x.get("status") == "verified")
        progress.emit(agent, "verifier", f"{incident}: {done}/{len(all_steps)} steps verified"
                      + (f" ({cur['op']} {status} in {took:.1f} s)" if took is not None else ""),
                      action_type=f"step:{cur['op']}", system=cur["system"], incident=incident, phase="done",
                      step=done, of=len(all_steps), elapsed_s=took, outcome=status)
    except Exception:
        pass


def verify(incident: str, step: dict, action_id: str, evidence: str, agent: str) -> bool:
    ok = _judge(incident, step, action_id, "verified", evidence, agent)
    if ok:
        _judged(incident, step, action_id, "verified", agent)
    if ok and lessons.enabled():
        try:  # failure memory: a retry after a failed attempt worked -> the lesson records what did
            lessons.note_recovery(incident, step, action_id)
        except Exception:
            pass
    return ok


def reject(incident: str, step: dict, action_id: str, evidence: str, agent: str) -> bool:
    ok = _judge(incident, step, action_id, "failed", evidence, agent)
    if ok:
        _judged(incident, step, action_id, "failed", agent)
    return ok
