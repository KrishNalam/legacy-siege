"""Swarm-only timing knobs (env-overridable). Shared timings live in common.config."""
from __future__ import annotations

import os

from common import config
from common.config import env_float as _f


THINK_S = 1.2                                   # one deliberate reasoning step (scaled by PACE)
POLL_S = _f("SWARM_POLL_S", 0.5)                # generic wait-loop poll interval
SENTINEL_POLL_S = _f("SENTINEL_POLL_S", 1.0)    # /health sweep interval
CONFIRM_POLLS = int(_f("SENTINEL_CONFIRM_POLLS", 2))  # consecutive unhealthy polls before acting
SETTLE_S = _f("SCOUT_SETTLE_S", 1.5)            # let the cascade's blast radius settle before recall
CASCADE_WAIT_S = _f("SCOUT_CASCADE_WAIT_S", 8)  # ...and give a healthy downstream dependent this long to follow
GUARD_GRACE_S = _f("GUARD_GRACE_S", 2.0)        # applied:true but state unchanged for this long => lie
DEAD_AFTER_S = _f("DEAD_AFTER_S", config.LEASE_S)   # Undertaker: last_seen older than this => dead
NURSE_POLL_S = _f("NURSE_POLL_S", 0.5)
NURSE_PICKUP_S = _f("NURSE_PICKUP_S", 2.0)      # open task with no live specialist this long => nurse takes it
FAILED_COOLDOWN_S = _f("FAILED_COOLDOWN_S", 30)  # after an escalation, don't re-open the same systems at once
RESOLVE_TIMEOUT_S = _f("RESOLVE_TIMEOUT_S", 60)
TASK_TIMEOUT_S = _f("TASK_TIMEOUT_S", 180)
MAX_ATTEMPTS = int(_f("MAX_ATTEMPTS", 4))
UPGRADE_AFTER = int(_f("UPGRADE_AFTER", 2))     # same fault_class resolved this many times => upgrade request
SESSION_HIGH = int(_f("SESSION_HIGH", 500))     # portal session store exhausted (degraded)
SESSION_OK = int(_f("SESSION_OK", 400))         # flush_sessions postcondition: below 80% of the store
SECURITY_SCAN_S = _f("SECURITY_SCAN_S", 3)      # sentinel scans logs for exploit signatures this often
DISK_HIGH = _f("DISK_HIGH", 90)


def step_hold_s() -> float:
    """Pause between marking a step 'sent' and POSTing it (the judge's kill window)."""
    v = os.environ.get("STEP_HOLD_S")
    return float(v) if v not in (None, "") else config.pace(THINK_S)
