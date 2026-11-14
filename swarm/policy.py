"""Deterministic policy: the dependency chain, who fixes what, and the fix plan for each fault class.
The LLM never decides which op runs — this module does."""
from __future__ import annotations

import re

from common import config

from . import settings

SPECIALTY = {"mainframe": "mainframe", "unix": "unix", "portal": "web", "console": "web"}

# Probe ids for planted vulnerabilities (CONTRACTS §2 /security/probe/{vuln_id}).
VULNS = {"mainframe": "mf-default-operator", "unix": "unix-world-writable-conf", "portal": "portal-sqli-search"}
EXPLOIT_CLASS = {"portal": "sqli_exploit", "unix": "config_exploit", "mainframe": "credential_exploit"}

# Log signatures of a vulnerability being USED (exploitable alone is a finding, not an incident).
EXPLOIT_SIGNS = {
    "portal": re.compile(r"(?i)(;\s*update\s+\w+\s+set|union\s+select|drop\s+table|'\s*or\s*'1'\s*=\s*'1)"),
    "unix": re.compile(r"(?i)(auditd.*(uid=65534|comm=\"sh\").*op=write|write as nobody succeeded)"),
    "mainframe": re.compile(r"(?i)OPERATOR.*(factory default|default password) accepted"),
}


def exploit_signs(sys: str, logs: list[dict]) -> list[str]:
    rx = EXPLOIT_SIGNS.get(sys)
    return [ln.get("line", "") for ln in logs or [] if rx and rx.search(str(ln.get("line", "")))]


def systems() -> list[str]:
    return list(config.SYSTEM_IDS)


def downstream_order() -> list[str]:
    """Systems ordered from most downstream (customer-facing) to most upstream: portal, unix, mainframe."""
    ids = systems()
    upstreams = {config.CHAIN.get(s) for s in ids}
    heads = [s for s in ids if s not in upstreams] or ids[:1]
    order: list[str] = []
    for h in heads:
        s = h
        while s and s in ids and s not in order:
            order.append(s)
            s = config.CHAIN.get(s)
    return order + [s for s in ids if s not in order]


def rank(sys: str) -> int:
    o = downstream_order()
    return o.index(sys) if sys in o else len(o)


def neighbours(sys: str) -> set[str]:
    ids = set(systems())
    n = {config.CHAIN.get(sys)} | {d for d in ids if config.CHAIN.get(d) == sys}
    return {x for x in n if x in ids}


def segments(unhealthy: list[str]) -> list[list[str]]:
    """Split unhealthy systems into contiguous chain segments, each ordered downstream-first."""
    left = set(unhealthy)
    out = []
    for s in downstream_order():
        if s not in left:
            continue
        seg, frontier = [], [s]
        while frontier:
            x = frontier.pop()
            if x in left:
                left.discard(x)
                seg.append(x)
                frontier.extend(neighbours(x))
        out.append(sorted(seg, key=rank))
    return out


def most_downstream(syss) -> str:
    return sorted(syss, key=rank)[0]


# ---------------------------------------------------------------- symptoms (canonical text => stable recall)
def symptom_line(sys: str, health: dict, metrics: dict | None) -> str:
    """One canonical, number-free line per unhealthy system so the same fault reads the same way twice."""
    m = metrics or {}
    parts = [f"{sys} {health.get('status', 'unknown')}"]
    parts.append(f"blames {health['blames']}" if health.get("blames") else "blames nothing upstream")
    if sys == "portal" and health.get("status") != "down":
        parts.append("still answering HTTP 200")
    if (m.get("error_rate") or 0) >= 0.05:
        parts.append("errors elevated")
    if (m.get("latency_ms") or 0) >= 1000:
        parts.append("latency high")
    if (m.get("disk_pct") or 0) >= settings.DISK_HIGH:
        parts.append("disk nearly full")
    if (m.get("sessions") or 0) >= settings.SESSION_HIGH:
        parts.append("session count high")
    if str(m.get("batch_status") or "").upper() == "ABENDED":
        parts.append("batch abended")
    return ", ".join(parts)


def symptom_text(symptoms: list[str]) -> str:
    return "; ".join(sorted(symptoms, key=lambda ln: (rank(ln.split(" ", 1)[0]), ln)))


def fmt_op(step: dict) -> str:
    """quarantine_record(record_id=REC-00042) — compact op label for narration and approvals."""
    p = step.get("params") or {}
    return step["op"] + (f"({', '.join(f'{k}={v}' for k, v in p.items())})" if p else "")


# ---------------------------------------------------------------- fix plans (CONTRACTS ops table)
def _step(system: str, op: str, params: dict, desc: str, destructive: bool = False) -> dict:
    return {"system": system, "op": op, "params": params, "desc": desc, "destructive": destructive}


DOWNSTREAM_REFRESH = [
    _step("unix", "rerun_transfer", {}, "Re-run the unix file transfer so it picks up a fresh batch file"),
    _step("portal", "refresh_cache", {}, "Refresh the portal balance cache from the fresh transfer"),
]


def fix_plan(fault_class: str, params: dict | None = None) -> list[dict]:
    """Ordered fix steps for a diagnosed fault. `params` carries facts from diagnosis (e.g. poison records)."""
    p = params or {}
    if fault_class == "batch_abort":
        recs = p.get("poison_records") or []
        steps = [_step("mainframe", "quarantine_record", {"record_id": r},
                       f"Quarantine malformed record {r} so NIGHTBAT can run") for r in recs]
        steps.append(_step("mainframe", "restart_batch", {"job": "NIGHTBAT"},
                           "Restart the NIGHTBAT batch job on the mainframe", destructive=True))
        return steps + DOWNSTREAM_REFRESH
    if fault_class == "disk_fill":
        return [_step("unix", "purge_spool", {"path": "/var/spool/xfer"}, "Purge the full xfer spool")] + DOWNSTREAM_REFRESH
    if fault_class == "cron_stop":
        return [_step("unix", "restart_cron", {"job": "xfer_pull"}, "Restart the silently stopped xfer_pull cron")] \
            + DOWNSTREAM_REFRESH
    if fault_class == "session_leak":
        return [_step("portal", "flush_sessions", {}, "Flush the leaked portal sessions")]
    if fault_class == "sqli_exploit":
        return [_step("portal", "apply_patch", {"patch": "parameterize_search"}, "Parameterize the portal search query")]
    if fault_class == "config_exploit":
        return [_step("unix", "chmod_config", {"file": "/etc/xfer.conf", "mode": "0640"},
                      "Make /etc/xfer.conf non-world-writable (0640)")]
    if fault_class == "credential_exploit":
        return [_step("mainframe", "rotate_credential", {"account": "OPERATOR"},
                      "Rotate the default OPERATOR credential", destructive=True)]
    return []


def rebind(fix: list[dict], fault_class: str, params: dict) -> list[dict]:
    """Turn a playbook's remembered fix into today's plan: facts (record ids) come from today's read-back."""
    if fault_class == "batch_abort":
        return fix_plan("batch_abort", params)
    remembered = [_step(f["system"], f["op"], dict(f.get("params") or {}), f.get("desc") or f"{f['op']} on {f['system']}",
                        bool(f.get("destructive"))) for f in fix]
    return remembered or fix_plan(fault_class)


DESTRUCTIVE = {("mainframe", "restart_batch"), ("mainframe", "rotate_credential")}

RISK = {
    "restart_batch": "Restarts the core-ledger NIGHTBAT job; batch window re-runs against production ledger.",
    "rotate_credential": "Rotates the production OPERATOR credential; anything still using the old one breaks.",
}

WEAKNESS = {
    "batch_abort": "NIGHTBAT accepts malformed fixed-width records (ABEND S0C7)",
    "disk_fill": "xfer spool has no rotation or quota; disk fills and transfers fail",
    "cron_stop": "xfer_pull cron can stop silently with no watchdog",
    "session_leak": "portal session store never expires sessions",
    "sqli_exploit": "portal search builds SQL from raw input",
    "config_exploit": "/etc/xfer.conf ships world-writable",
    "credential_exploit": "mainframe OPERATOR account keeps its default credential",
}
