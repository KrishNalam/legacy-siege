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
