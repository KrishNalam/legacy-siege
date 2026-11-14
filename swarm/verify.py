"""Postconditions from the CONTRACTS ops table, checked by READ-BACK (never by trusting the op's ACK).

check(op, params, read) -> (applied: bool, evidence: str)
`read(sys)` returns that system's /state; `probe(sys, vuln)` runs a security probe.
Used by the Guard (to verify) and by Nurses (to reconcile a step stuck in 'sent').
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import settings
from .policy import VULNS


def _ts(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _probe_clean(probe, sys: str) -> tuple[bool, str]:
    vid = VULNS[sys]
    try:
        r = probe(sys, vid)
    except Exception as e:
        return False, f"probe {vid} failed: {e}"
    return (not r.get("exploitable"), f"probe {vid}: exploitable={bool(r.get('exploitable'))}")


def check(op: str, params: dict, read, probe=None, sent_at: datetime | None = None) -> tuple[bool, str]:
    p = params or {}
    if op == "quarantine_record":
        b = read("mainframe").get("batch", {})
        rid = p.get("record_id")
        ok = rid in (b.get("quarantined") or []) and rid not in (b.get("poison_records") or [])
        return ok, f"state.batch.quarantined={b.get('quarantined')}; poison_records={b.get('poison_records')}"
    if op == "restart_batch":
        b = read("mainframe").get("batch", {})
        last = _ts(b.get("last_run_ok"))
        fresh = last is not None and (sent_at is None or last >= sent_at - timedelta(seconds=5))
        return b.get("status") == "OK" and fresh, f"state.batch.status={b.get('status')}; last_run_ok={b.get('last_run_ok')}"
    if op == "rotate_credential":
        cred = read("mainframe").get("credentials", {}).get(p.get("account", "OPERATOR"))
        clean, pf = _probe_clean(probe, "mainframe") if probe else (True, "no probe")
        return cred == "rotated" and clean, f"state.credentials.OPERATOR={cred}; {pf}"
    if op == "purge_spool":
        d = read("unix").get("spool", {}).get("disk_pct")
        return d is not None and d < 80, f"state.spool.disk_pct={d}"
    if op == "restart_cron":
        c = read("unix").get("cron", {}).get("xfer_pull")
        return c == "running", f"state.cron.xfer_pull={c}"
    if op == "chmod_config":
        f = p.get("file", "/etc/xfer.conf")
        mode = read("unix").get("config", {}).get(f)
        clean, pf = _probe_clean(probe, "unix") if probe else (True, "no probe")
        return mode == p.get("mode", "0640") and clean, f"state.config[{f}]={mode}; {pf}"
    if op == "rerun_transfer":
        t = read("unix").get("transfer", {})
        mf = read("mainframe").get("batch", {})
        ft, mt = _ts(t.get("last_file_batch_ts")), _ts(mf.get("last_run_ok"))
        ok = ft is not None and mt is not None and ft >= mt and mf.get("status") == "OK"
        return ok, f"state.transfer.last_file_batch_ts={t.get('last_file_batch_ts')} >= mainframe.last_run_ok={mf.get('last_run_ok')}"
    if op == "flush_sessions":
        n = read("portal").get("sessions")
        return n is not None and n < settings.SESSION_OK, f"state.sessions={n} (< {settings.SESSION_OK})"
    if op == "apply_patch":
        patch = read("portal").get("search_patch")
        clean, pf = _probe_clean(probe, "portal") if probe else (True, "no probe")
        return patch == p.get("patch", "parameterize_search") and clean, f"state.search_patch={patch}; {pf}"
    if op == "map_field":
        from . import schema  # noqa: PLC0415
        return schema.verify_mapping(p.get("field", "balance"), p.get("source"), read("portal"), read("unix"))
    if op == "refresh_cache":
        c = read("portal").get("cache", {})
        return c.get("stale") is False, f"state.cache.stale={c.get('stale')}; balances_as_of={c.get('balances_as_of')}"
    return False, f"no postcondition known for {op}"
