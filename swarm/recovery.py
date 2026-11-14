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


DYNAMIC_FACT_CLASSES = ('batch_abort', 'schema_drift')  # today's read-back facts replace the remembered ones


def vet_recall(incident: str, hits: list[dict], summary: list[dict], usable: dict | None, query: str,
               agent: str) -> tuple[dict | None, list[str]]:
    """Provenance check at recall time for playbooks whose remembered facts are re-derived today
    (wrong-record memory poisoning would otherwise be silently neutralised, never seen).

    A playbook's fix must match the steps the Guard verified in the incident it was learned from. If it
    doesn't, the memory was tampered with: retire it (stale), store a misfit lesson, and fall back to the
    next usable hit (or the chain walk). Returns (usable, narration lines). Same switches as
    ``reject_bad_memory``.
    """
    from common.db import col, now
    from . import lessons

    if not enabled() or not lessons.enabled():
        return usable, []
    sig = lambda s: (s.get('system'), s.get('op'), s.get('params') or {})
    lines, rejected = [], set()
    for row in summary:
        if not row.get('usable') or row.get('fault_class') not in DYNAMIC_FACT_CLASSES:
            continue
        pb = col('playbooks').find_one({'_id': row['playbook_id']}, {'embedding': 0})
        src = pb and pb.get('incident') and col('incidents').find_one({'_id': pb['incident']}, {'steps': 1})
        if not src:
            continue  # no provenance to judge by (incident history wiped): keep the memory
        verified = [sig(s) for s in src.get('steps') or [] if not s.get('superseded_by_replan')]
        remembered = [sig(s) for s in pb.get('fix') or []]
        if remembered == verified:
            continue
        bad = next((r for r in remembered if r not in verified), remembered[0] if remembered else None)
        evidence = (f"remembered {bad[1]} {bad[2]} on {bad[0]} was never verified in {pb['incident']}"
                    if bad else f"fix differs from what was verified in {pb['incident']}")
        col('playbooks').update_one({'_id': pb['_id']}, {'$set': {
            'stale': True, 'staled_at': now(), 'stale_reason': f'Rejected memory at recall: {evidence[:180]}'}})
        lessons.record_misfit(incident, {'playbook_id': pb['_id'], 'system': pb.get('system'),
                                         'fault_class': pb.get('fault_class')},
                              query, {'facts': [evidence], 'fault_class': 'a tampered memory'}, agent,
                              pb.get('system_version'))
        row.update(usable=False, stale=True, rejected=True)
        rejected.add(pb['_id'])
        lines.append(f"Rejected memory {pb['_id']}: {evidence}. Its fix doesn't match what the Guard verified; "
                     "retiring it as stale and planning from today's read-back instead.")
    if usable is not None and usable['_id'] in rejected:
        ok = {r['playbook_id'] for r in summary if r.get('usable')}
        usable = next((h for h in hits if h['_id'] in ok), None)
    return usable, lines
