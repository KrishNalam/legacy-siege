"""Deterministic diagnosis rules: what a specialist concludes from one system's health/metrics/logs/state.

Used by worker diagnose tasks (one hop of the chain walk) and by the scout to confirm a recalled playbook.
"""
from __future__ import annotations

import re

from . import settings
from .policy import EXPLOIT_CLASS, VULNS, exploit_signs

REC = re.compile(r"\bREC-\d+\b")


def _schema_drift(state: dict) -> dict | None:
    """Compare unix's manifest sample with the Hive's expected schema (MongoDB $jsonSchema + field diff).
    Never lets a schema-check problem break a diagnosis."""
    if not ((state or {}).get("feed") or {}).get("sample"):
        return None
    try:
        from . import schema  # noqa: PLC0415
        return schema.detect(state)
    except Exception:
        return None


def classify(system: str, health: dict, metrics: dict, logs: list[dict], state: dict,
             probe=None) -> dict:
    """Return findings for one system:
    {system, status, blames, fault_class|None, root_cause|None, facts:[str], params:{}}

    Exploits are checked last: a vuln counts only if the probe says exploitable AND the logs show it used.
    """
    facts: list[str] = []
    fault, cause, params = None, None, {}
    text = "\n".join(f"{ln.get('code', '')} {ln.get('line', '')}" for ln in logs or [])

    if system == "mainframe":
        batch = (state or {}).get("batch") or {}
        bstat = str(batch.get("status") or metrics.get("batch_status") or "").upper()
        recs = list(batch.get("poison_records") or []) or sorted(set(REC.findall(text)))
        # an ABENDED batch, or a recent S0C7 with a poison record still queued
        if bstat == "ABENDED" or ("S0C7" in text and batch.get("poison_records")):
            fault, params = "batch_abort", {"poison_records": recs}
            cause = f"NIGHTBAT ABEND S0C7: malformed fixed-width record {', '.join(recs) or '(unknown)'}"
            abend = next((ln.get("line", "") for ln in logs or [] if "S0C7" in str(ln.get("line", ""))), None)
            facts += [f"log: {abend[:90]}" if abend else "batch_status=ABENDED",
                      f"poison_records={recs}", f"last_batch_ok={metrics.get('last_batch_ok')}"]
    elif system == "unix":
        disk = ((state or {}).get("spool") or {}).get("disk_pct", metrics.get("disk_pct")) or 0
        cron = ((state or {}).get("cron") or {}).get("xfer_pull")
        if disk >= settings.DISK_HIGH:
            fault, cause = "disk_fill", f"xfer spool disk at {disk}%: transfers cannot land"
            facts.append(f"spool.disk_pct={disk}")
        elif cron == "stopped":
            fault, cause = "cron_stop", "xfer_pull cron stopped silently: no transfer activity"
            facts.append("cron.xfer_pull=stopped")
        else:
            t = (state or {}).get("transfer") or {}
            facts.append(f"transfer.status={t.get('status')} last_file_batch_ts={t.get('last_file_batch_ts')}")
            drift = _schema_drift(state)
            if drift:
                fault, cause, params = drift["fault_class"], drift["root_cause"], drift["params"]
                facts += drift["facts"]
    elif system == "portal":
        sessions = (state or {}).get("sessions", metrics.get("sessions")) or 0
        if sessions >= settings.SESSION_HIGH:
            fault, cause = "session_leak", f"portal session store holds {sessions} sessions"
            facts.append(f"sessions={sessions}")
        feed = (state or {}).get("feed") or {}
        if feed.get("errors"):
            facts.append(f"xfer manifest unreadable: <{(feed.get('map') or {}).get('balance')}> missing "
                         f"({feed['errors']} unmarshal errors)")
        cache = (state or {}).get("cache") or {}
        if cache.get("stale"):
            facts.append(f"serving stale balances as of {cache.get('balances_as_of')} (HTTP 200)")

    if fault is None and probe is not None and not health.get("blames"):
        vid = VULNS.get(system)
        signs = exploit_signs(system, logs)
        if vid and signs:
            try:
                pr = probe(system, vid)
            except Exception:
                pr = None
            if pr and pr.get("exploitable"):
                fault = EXPLOIT_CLASS[system]
                cause = f"{vid} exploitable and exploited: {signs[-1][:120]}"
                facts += [f"log: {signs[-1][:90]}", f"probe {vid}: exploitable ({str(pr.get('detail', ''))[:60]})"]

    return {"system": system, "status": health.get("status"), "blames": health.get("blames"),
            "fault_class": fault, "root_cause": cause, "facts": facts, "params": params}
