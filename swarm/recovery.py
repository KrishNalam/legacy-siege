"""Visible recovery when an attack (e.g. the Red fleet) outlasts the normal attempt budget.

Switch: ``SWARM_RECOVERY`` (default: on when ``RED_FLEET`` is on, otherwise off, so the seeded demo is unchanged).

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
    return _on("SWARM_RECOVERY", "1" if _on("RED_FLEET") else "")


ROUNDS = int(_f("RECOVERY_ROUNDS", 2))          # fresh attempt budgets before escalating to the Beekeeper
BACKOFF_S = _f("RECOVERY_BACKOFF_S", 3.0)       # x round number: let late ops land / injected lies drain


def budget(step: dict) -> int:
    """How many attempts this step may use (a recovery round raises it)."""
    return int(step.get("max_attempts") or settings.MAX_ATTEMPTS)


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
    canonical = (inc.get('steps') or []) if fc in ('batch_abort', 'schema_drift') else policy.fix_plan(fc)
    signature = lambda s: (s['system'], s['op'], s.get('params') or {})
    if not pb or not canonical or [signature(s) for s in pb.get('fix') or []] == [signature(s) for s in canonical]:
        return None
    col('playbooks').update_one({'_id': pb['_id']}, {'$set': {
        'stale': True, 'staled_at': now(), 'stale_reason': f'Guard rejected recalled repair: {evidence[:180]}'}})
    lessons.record_misfit(incident, {'playbook_id': pb['_id'], 'system': pb['system'], 'fault_class': fc},
                          rec.get('query', ''), {'facts': [evidence], 'fault_class': 'an unverified repair'}, agent, pb['system_version'])
    col('incidents').update_one({'_id': incident}, {'$set': {'recall.rejected': pb['_id']}})
    return pb['_id']
