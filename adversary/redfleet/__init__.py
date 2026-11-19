"""Red fleet: AI attacker agents on top of the seeded Chaos Monkey (opt-in, ``RED_FLEET=1``).

Five attackers, each its own OS process running its own LangGraph loop (scout -> plan -> strike -> observe):

    red-saboteur  legacy breakage mid-repair: batch_abort, disk_fill, cron_stop, session_leak
    red-assassin  SIGKILLs bees mid-task (a worker mid-send, the crowned queen, a scout, the guard ...)
    red-liar      false reports: lying_success, slow_apply, false_log (misleading lines blaming the wrong system)
    red-hacker    exploits the planted vulns: credential_exploit, sqli_exploit, config_exploit
    red-drifter   renames fields under the Hive: schema_drift

Claude picks ONE item from the persona's menu (built from the existing fault library and the Hive's state
in MongoDB); anything not on the menu is rejected and the persona's deterministic playbook decides instead
(also with no key, on a timeout or bad JSON). Strikes only go through ``adversary.runner.fire``.

    RED_FLEET=1 python -m adversary.redfleet                       # the fleet (run.py starts it when RED_FLEET=1)
    RED_FLEET=1 python -m adversary.redfleet --agent red-liar      # one attacker
"""
from .core import Settings, enabled, gate, live_db_refusal, parse_json, scout  # noqa: F401
from .personas import PERSONAS, Persona, validate  # noqa: F401
