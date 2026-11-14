"""Visible recovery when an attack (e.g. the Red fleet) outlasts the normal attempt budget.

Switch: ``SWARM_RECOVERY`` (default off, independent of ``RED_FLEET``: a Red stage run keeps the seeded lie and
round-2 timing unless recovery is switched on explicitly).

With it on:
  * a fix task that ran out of attempts (or timed out) is NOT escalated at once: the scout backs off,
    re-reads the system, and re-dispatches the unverified steps with a fresh attempt budget
    (``RECOVERY_ROUNDS`` rounds, each ``MAX_ATTEMPTS`` more attempts); only then does it escalate;
  * before a worker opens a new attempt it reconciles by read-back: if an earlier attempt has landed
    late (slow_apply), it re-submits that attempt to the Guard instead of sending the op again.
Every decision is narrated, so the attack -> recovery is visible in the war room's thought stream.
"""
from __future__ import annotations

import os

from common.config import env_float as _f

from . import settings


def _on(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    return _on("SWARM_RECOVERY")


ROUNDS = int(_f("RECOVERY_ROUNDS", 3))          # recovery rounds (re-diagnose + fresh budget or replan), then escalate
FIRST_BUDGET = int(_f("RECOVERY_FIRST_BUDGET", 2))  # attempts before the first re-diagnosis (each can cost ~30 s)
BACKOFF_S = _f("RECOVERY_BACKOFF_S", 3.0)       # base backoff, doubled each round: let late ops land / lies drain
BACKOFF_MAX_S = _f("RECOVERY_BACKOFF_MAX_S", 20.0)
HEALTH_GRACE_S = _f("RECOVERY_HEALTH_GRACE_S", 8.0)  # steps verified but a system still down this long: re-diagnose


def backoff_s(rnd: int) -> float:
    """Exponential backoff for recovery round `rnd` (1-based), capped."""
    return min(BACKOFF_S * (2 ** max(rnd - 1, 0)), BACKOFF_MAX_S)


def rediagnose(sysc, system: str, since: str | None = None, max_hops: int = 3) -> dict | None:
    """Re-read the system a failing step targets (and what it blames, upstream) for a fault that landed
    mid-repair. Returns the first diagnosis with a fault class, or None if nothing new is wrong."""
    from . import diagnosis

    seen, sys = set(), system
    while sys and sys not in seen and len(seen) < max_hops:
        seen.add(sys)
        try:
            h, m = sysc.health(sys), sysc.metrics(sys)
            f = diagnosis.classify(sys, h, m, sysc.logs(sys, limit=50, since=since), sysc.state(sys),
                                   probe=sysc.probe)
        except Exception:
            return None
        if f.get("fault_class"):
            return f
        sys = f.get("blames")
    return None


def replan_steps(steps: list[dict], inc_id: str, findings: dict, ts) -> tuple[list[dict], list[dict]] | None:
    """A new fault blocks the plan: put its policy remediation in front of every unverified step.

    Unverified steps are kept as superseded evidence and re-appended (fresh ids, fresh attempt budget) after
    the remediation. Remediation steps equal to a still-pending step are not duplicated. Returns
    (new full step list, the remediation steps added) or None when policy has no fix for the new fault."""
    from . import policy

    raw = policy.fix_plan(findings["fault_class"], findings.get("params") or {})
    if not raw:
        return None
    sig = lambda s: (s["system"], s["op"], s.get("params") or {})  # noqa: E731
    old = [dict(s) for s in steps]
    pending = [s for s in sorted(old, key=lambda s: s["n"])
               if s["status"] != "verified" and not s.get("superseded_by_replan")]
    pend_sigs = [sig(s) for s in pending]
    for s in pending:
        s["superseded_by_replan"] = True
    n = max((s["n"] for s in old), default=0)
    added, out = [], old
    fresh = {"task_id": None, "status": "planned", "action_id": None, "attempts": [], "agent": None,
             "evidence": None, "approval_id": None, "max_attempts": None, "ts": ts}
    for s in raw:
        if sig(s) in pend_sigs:
            continue
        n += 1
        st = {**s, "destructive": bool(s["destructive"] or (s["system"], s["op"]) in policy.DESTRUCTIVE),
              "step_id": f"{inc_id}-s{n}", "n": n, "replan_for": findings["fault_class"], **fresh}
        out.append(st)
        added.append(st)
    if not added:
        return None
    for s in pending:
        n += 1
        keep = {k: v for k, v in s.items() if k not in fresh and k not in ("superseded_by_replan", "judged_at")}
        out.append({**keep, "step_id": f"{inc_id}-s{n}", "n": n, "retry_of": s["step_id"], **fresh})
    return out, added


def budget(step: dict) -> int:
    """How many attempts this step may use (a recovery round raises it)."""
    return int(step.get("max_attempts") or (max(FIRST_BUDGET, 1) if enabled() else settings.MAX_ATTEMPTS))


def why_failed(steps: list[dict]) -> str:
    """Short, plain reason from the Guard's evidence on the failed steps (for the narration)."""
    evidence = [str(s.get("evidence") or "") for s in steps]
    parts = []
    if any("ACK ≠ APPLIED" in ev for ev in evidence):
        parts.append("the system kept ACKing without applying (the Guard caught each one)")
    if any("never applied" in ev or "no ACK" in ev for ev in evidence):
        parts.append("an accepted op never landed in time")
    return "; ".join(parts) or "the attempts did not take effect"


def reject_bad_memory(incident: str, evidence: str, agent: str) -> str | None:
    """Retire a recalled plan only when read-back failed AND it disagrees with the static repair policy.

    A legitimate plan suffering a lying ACK is kept. Dynamic-fact plans compare against the live rebind.
    """
    from common.db import col, now
    from . import lessons, policy

    if not enabled() or not lessons.enabled():
        return None
    inc = col('incidents').find_one({'_id': incident}) or {}
    rec = inc.get('recall') or {}
    fc = inc.get('fault_class')
    if not rec.get('used') or fc is None:
        return None
    hits = [h for h in rec.get('hits', []) if h.get('usable')]
    if not hits:
        return None
    pb = col('playbooks').find_one({'_id': hits[0]['playbook_id']})
    if not pb:
        return None
    if fc in ('batch_abort', 'schema_drift'):
        # Dynamic-fact classes: today's record ids / field names always differ from the remembered ones (and
        # rebind replaced them anyway), so judge only WHAT the memory does: the same (system, op) kinds as policy.
        canonical = inc.get('steps') or []
        same = {(s['system'], s['op']) for s in pb.get('fix') or []} == {(s['system'], s['op']) for s in canonical}
    else:
        canonical = policy.fix_plan(fc)
        signature = lambda s: (s['system'], s['op'], s.get('params') or {})  # noqa: E731
        same = [signature(s) for s in pb.get('fix') or []] == [signature(s) for s in canonical]
    if not canonical or same:
        return None
    col('playbooks').update_one({'_id': pb['_id']}, {'$set': {
        'stale': True, 'staled_at': now(), 'stale_reason': f'Guard rejected recalled repair: {evidence[:180]}'}})
    lessons.record_misfit(incident, {'playbook_id': pb['_id'], 'system': pb['system'], 'fault_class': fc},
                          rec.get('query', ''), {'facts': [evidence], 'fault_class': 'an unverified repair'}, agent, pb['system_version'])
    col('incidents').update_one({'_id': incident}, {'$set': {'recall.rejected': pb['_id']}})
    return pb['_id']
