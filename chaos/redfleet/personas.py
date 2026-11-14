"""The five Red attackers. Each one knows a slice of the fault library and builds a menu from the Hive's
current state; Claude (or the persona's own deterministic playbook) picks ONE item; anything not on the
menu is rejected. Params always come from the menu, never from the model."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from common import config
from common.db import now

from .. import faults
from .core import NEVER_KILL, Settings

SYSTEMS = tuple(config.SYSTEM_IDS)
VULN_OF = {"sqli_exploit": "portal-sqli-search", "config_exploit": "unix-world-writable-conf",
           "credential_exploit": "mf-default-operator"}
DRIFT_NAMES = ("acct_bal", "ledger_bal", "bal_amt", "avail_bal", "cur_balance")
# which fault on which system hurts a pending fix step the most (compound faults mid-repair)
SPOILER = {"mainframe": "batch_abort", "unix": "disk_fill", "portal": "session_leak"}


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    motto: str
    question: str                 # the Statement 3 angle it attacks
    faults: tuple[str, ...]
    stagger_s: float              # first strike this long after a night starts (+ RED_FLEET_FIRST_STRIKE_S)
    menu_fn: Callable[[dict], list[dict]] = field(repr=False)
    policy_fn: Callable[[dict, list[dict]], dict | None] = field(repr=False)

    def menu(self, snap: dict, cfg: Settings) -> list[dict]:
        allowed = set(cfg.allowed) if cfg.allowed else None
        out = []
        for item in self.menu_fn(snap):
            if item["fault"] not in faults.FAULTS or item["fault"] not in self.faults:
                continue
            if not faults.FAULTS[item["fault"]].enabled():
                continue
            if allowed is not None and item["fault"] not in allowed:
                continue
            if item["fault"] in ("agent_kill", "agent_freeze") and cfg.victims and not _victim_ok(item, cfg.victims):
                continue
            busy = any(b.get("_id") != self.id and b.get("status") == "striking"
                       and (b.get("intent") or {}).get("fault") == item["fault"]
                       and (b.get("intent") or {}).get("target") == item["target"]
                       and b.get("last_seen") and (now() - b["last_seen"]).total_seconds() < 60
                       for b in snap.get("red_board", []))
            if not busy:
                out.append(item)
        return out


def _victim_ok(item: dict, patterns: tuple[str, ...]) -> bool:
    role, phase = item.get("role"), item.get("phase")
    return any(p == role or p == f"{role}:{phase}" for p in patterns)


# ------------------------------------------------------------------ helpers
def _used(snap: dict) -> list[str]:
    return [a["fault"] for a in snap.get("red_attacks") or []]


def _pending_systems(snap: dict) -> list[str]:
    seen: list[str] = []
    for inc in snap.get("incidents") or []:
        for st in inc.get("pending_steps") or []:
            if st.get("system") and st["system"] not in seen:
                seen.append(st["system"])
    return seen


def _pick(menu: list[dict], **want) -> dict | None:
    return next((m for m in menu if all(m.get(k) == v for k, v in want.items())), None)


def _with(item: dict | None, reason: str) -> dict | None:
    return None if item is None else {**item, "reason": reason}


# ------------------------------------------------------------------ Saboteur: legacy breakage, mid-repair
def saboteur_menu(snap: dict) -> list[dict]:
    return [{"fault": "batch_abort", "target": "mainframe"}, {"fault": "disk_fill", "target": "unix"},
            {"fault": "cron_stop", "target": "unix"}, {"fault": "session_leak", "target": "portal"}]


def saboteur_policy(snap: dict, menu: list[dict]) -> dict | None:
    for sys in _pending_systems(snap):   # break the very system the Hive is about to fix next
        item = _pick(menu, fault=SPOILER.get(sys), target=sys)
        if item and SPOILER[sys] not in (snap.get("systems", {}).get(sys, {}).get("faults_active") or []):
            return _with(item, f"the Hive's plan still needs {sys}; breaking it mid-repair forces a re-plan")
    used = _used(snap)
    for item in menu:
        if item["fault"] not in used:
            return _with(item, "the estate looks calm; open a new front in the chain")
    return None


# ------------------------------------------------------------------ Assassin: crashes the bees
ARMED = "next-worker-mid-send"   # like the war room's Kill button: strike the next worker that sends a step


def assassin_menu(snap: dict) -> list[dict]:
    agents = snap.get("agents") or []
    queens = [a for a in agents if a["role"] == "queen"]
    out = []
    if _pending_systems(snap) and not snap.get("mid_send"):
        out.append({"fault": "agent_kill", "target": "swarm", "victim": ARMED, "role": "worker",
                    "phase": "step_sent", "incident": (snap.get("incidents") or [{}])[0].get("id")})
    for a in agents:
        role = a.get("role")
        if role in NEVER_KILL or role in ("waggle",):
            continue
        if role == "queen":
            if a.get("status") == "standby" or len(queens) < 2:
                continue   # never the last queen standing; only the crowned one while a standby waits
        elif role == "nurse" and a.get("status") != "working":
            continue
        elif role == "worker" and a.get("status") != "working":
            continue
        out.append({"fault": "agent_kill", "target": "swarm", "victim": a["id"], "role": role,
                    "phase": a.get("phase"), "incident": a.get("incident")})
    out += [{**m, "fault": "agent_freeze"} for m in out if m.get("role") == "worker"]
    return out


def assassin_policy(snap: dict, menu: list[dict]) -> dict | None:
    for item in menu:
        if item["role"] == "worker" and item.get("phase") == "step_sent" and item["victim"] != ARMED:
            return _with(item, f"{item['victim']} is mid-send on {item.get('incident')}; a crash now tests "
                               "resume-from-checkpoint without replaying")
    armed = _pick(menu, victim=ARMED)
    if armed:
        return _with(armed, f"{armed.get('incident')} still has steps to send; I kill whichever worker sends next, "
                            "mid-step")
    order = ("scout", "queen", "guard", "nurse", "worker", "sentinel")
    for role in order:
        for item in menu:
            if item["role"] == role and (role not in ("scout", "worker", "nurse") or item.get("incident")):
                return _with(item, f"{item['victim']} holds the Hive's {role} lease; take it out and see who "
                                   "picks the work up")
    return None


# ------------------------------------------------------------------ Liar: false reports
def liar_menu(snap: dict) -> list[dict]:
    out = []
    for s in SYSTEMS:
        out += [{"fault": "lying_success", "target": s}, {"fault": "slow_apply", "target": s}]
        out += [{"fault": "false_log", "target": s, "blame": b} for b in SYSTEMS if b != s]
    out += [{"fault": "lie_to_agent", "target": s} for s in SYSTEMS]
    return out


def liar_policy(snap: dict, menu: list[dict]) -> dict | None:
    used = _used(snap)
    pending = _pending_systems(snap)
    if pending:
        s = pending[0]
        honesty = "lying_success" if used.count("lying_success") <= used.count("slow_apply") else "slow_apply"
        return _with(_pick(menu, fault=honesty, target=s),
                     f"the Hive's next step lands on {s}; its ACK won't mean APPLIED")
    for inc in snap.get("incidents") or []:
        root = inc.get("root_cause_system") or inc.get("system")
        wrong = next((b for b in reversed(SYSTEMS) if b != root), None)
        item = _pick(menu, fault="false_log", target=root, blame=wrong)
        if item:
            return _with(item, f"plant an operator note on {root} blaming {wrong}; a log-reader walks the wrong way")
    return _with(_pick(menu, fault="false_log", target="mainframe", blame="unix"),
                 "a fake ABEND on the mainframe blaming unix: does anyone trust logs over read-back?")


# ------------------------------------------------------------------ Hacker: exploits the planted vulns
def hacker_menu(snap: dict) -> list[dict]:
    target = {"sqli_exploit": "portal", "config_exploit": "unix", "credential_exploit": "mainframe"}
    out = []
    for fault, vuln in VULN_OF.items():
        sysdoc = snap.get("systems", {}).get(target[fault])
        if sysdoc is not None and vuln not in (sysdoc.get("open_vulns") or []):
            continue   # already patched: the exploit would only bounce
        out.append({"fault": fault, "target": target[fault]})
    return out


def hacker_policy(snap: dict, menu: list[dict]) -> dict | None:
    used = _used(snap)
    fresh = [m for m in menu if m["fault"] not in used] or menu
    order = ("credential_exploit", "sqli_exploit", "config_exploit")
    for f in order:
        item = _pick(fresh, fault=f)
        if item:
            return _with(item, f"{VULN_OF[f]} is still open; walk in while the Hive is busy elsewhere")
    return None


# ------------------------------------------------------------------ Drifter: the layout changes underneath
def drifter_menu(snap: dict) -> list[dict]:
    current = snap.get("systems", {}).get("unix", {}).get("feed_field")
    return [{"fault": "schema_drift", "target": "unix", "to": n} for n in DRIFT_NAMES if n != current] + \
           [{"fault": "schema_drift", "target": "unix"}]


def drifter_policy(snap: dict, menu: list[dict]) -> dict | None:
    if "schema_drift" in (snap.get("systems", {}).get("unix", {}).get("faults_active") or []):
        return None   # the last rename hasn't been adapted to yet; piling on just hides the signal
    return _with(_pick(menu, fault="schema_drift", target="unix", to=None) or (menu[0] if menu else None),
                 "a vendor patch renames the balance field; unix stays green while the portal chokes")


# ------------------------------------------------------------------ Memory Striker: poisons what the Hive learned
POISON_FOR = {"disk_fill": "unix", "cron_stop": "unix", "session_leak": "portal",
              "config_exploit": "unix", "sqli_exploit": "portal", "credential_exploit": "mainframe"}


def memory_menu(snap: dict) -> list[dict]:
    """One item per genuine learned repair the Hive would replay as remembered (victim = that playbook)."""
    return [{"fault": "poison_memory", "target": "swarm", "victim": pb["id"], "fault_class": pb.get("fault_class")}
            for pb in snap.get("poisonable") or []]


def memory_policy(snap: dict, menu: list[dict]) -> dict | None:
    used = {a.get("victim") for a in snap.get("red_attacks") or []}
    item = next((m for m in menu if m["victim"] not in used), None)
    return _with(item, f"the Hive will replay its {item['fault_class']} repair from memory; aim it at the wrong "
                       "system and see whether read-back or memory wins") if item else None


PERSONAS: dict[str, Persona] = {p.id: p for p in [
    Persona("red-saboteur", "Saboteur", "legacy breakage: ABENDs, full disks, dead crons, leaking sessions",
            "systems won't hold still: compound faults mid-repair",
            ("batch_abort", "disk_fill", "cron_stop", "session_leak"), 25, saboteur_menu, saboteur_policy),
    Persona("red-assassin", "Assassin", "SIGKILLs bees mid-task",
            "Q2 resume after a crash without replaying", ("agent_kill", "agent_freeze"), 0, assassin_menu, assassin_policy),
    Persona("red-liar", "Liar", "ACKs that don't apply, late applies, false log lines",
            "Q1 took effect vs only sent; knowing what actually happened",
            ("lying_success", "slow_apply", "false_log", "lie_to_agent"), 10, liar_menu, liar_policy),
    Persona("red-hacker", "Hacker", "walks in through the planted vulns",
            "an intruder mid-job: detect, patch, retest", tuple(VULN_OF), 40, hacker_menu, hacker_policy),
    Persona("red-drifter", "Drifter", "silently renames fields under the Hive",
            "Q3 adapt when fields change underneath it", ("schema_drift",), 55, drifter_menu, drifter_policy),
    Persona("red-memory", "Memory Striker", "poisons what the Hive learned: a remembered repair aimed at the wrong system",
            "Q2/Q3 memory you can't trust: read-back must beat recall", ("poison_memory",), 30, memory_menu, memory_policy),
]}

KEYS = ("fault", "target", "victim", "blame", "to")


def validate(persona: Persona, choice, snap: dict, cfg: Settings) -> tuple[dict | None, str]:
    """A choice is legal only if it is a library fault AND exactly an item on this persona's menu right now.
    Returns (strike, "ok") with params built from the menu, or (None, why)."""
    if not isinstance(choice, dict):
        return None, "not an object"
    name = choice.get("fault")
    if not isinstance(name, str) or name not in faults.FAULTS:
        return None, f"{name!r} is not in the fault library"
    if name not in persona.faults:
        return None, f"{name} is not in the {persona.name}'s kit"
    menu = persona.menu(snap, cfg)
    key = {k: choice.get(k) for k in KEYS if k in choice and choice.get(k) is not None}
    item = next((m for m in menu if {k: m[k] for k in KEYS if k in m} == key), None)
    if item is None:
        return None, f"{key} is not on the menu right now"
    f = faults.FAULTS[name]
    try:
        target = faults.resolve_target(f, item["target"])
    except faults.FaultError as e:
        return None, str(e)
    params: dict = {}
    if name in ("agent_kill", "agent_freeze"):
        if item["victim"] == ARMED:
            params["selector"] = {"role": "worker", "phase": "step_sent"}
        elif item.get("role") == "worker" and item.get("phase") == "step_sent":
            spec = next((a.get("specialty") for a in snap.get("agents") or [] if a["id"] == item["victim"]), None)
            params["selector"] = {"role": "worker", "phase": "step_sent", **({"specialty": spec} if spec else {})}
        else:
            params["selector"] = {"agent": item["victim"]}
        params["wait_s"] = max(cfg.kill_wait_s, 25.0) if item["victim"] == ARMED else cfg.kill_wait_s
    elif name == "lying_success":
        params = {"count": 1}
    elif name == "false_log":
        params = {"blame": item["blame"]}
    elif name == "poison_memory":
        params = {"playbook": item["victim"]}
    elif name == "schema_drift" and item.get("to"):
        params = {"to": item["to"]}
    reason = choice.get("reason") if isinstance(choice.get("reason"), str) else ""
    return {"fault": name, "target": target, "params": params, "victim": item.get("victim"),
            "question": persona.question, "reason": reason.strip()[:500]}, "ok"
