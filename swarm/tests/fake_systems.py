"""A small, deterministic, in-memory FAKE of the legacy estate (CONTRACTS §2 subset) for swarm tests.

    .venv/bin/python -m uvicorn swarm.tests.fake_systems:app --port 18731

Implements health/metrics/logs/ops/state/probe, the batch_abort cascade (mainframe ABEND → unix stale
transfer → portal stale cache, health still "up" on the mainframe), lying_success, slow_apply, op receipts
keyed by action_id (same id → stored receipt, never applied twice), admin reset/reload, plus test-only
introspection at GET /_fake/receipts (receipts, POST counts per action_id, effective applications per op).
"""
from __future__ import annotations

import os
import threading
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Body, FastAPI, HTTPException

TICK_S = float(os.environ.get("FAKE_TICK_S", 0.2))
SLOW_APPLY_S = float(os.environ.get("SLOW_APPLY_S", 20))
SYSTEMS = ("mainframe", "unix", "portal")

OPS = {
    "mainframe": [("quarantine_record", False), ("restart_batch", True), ("rotate_credential", True)],
    "unix": [("purge_spool", False), ("restart_cron", False), ("chmod_config", False), ("rerun_transfer", False)],
    "portal": [("flush_sessions", False), ("apply_patch", False), ("refresh_cache", False)],
}

LOCK = threading.RLock()
W: dict = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def baseline() -> None:
    t = _now()
    W.clear()
    W.update({
        "version": {s: 1 for s in SYSTEMS},
        "mainframe": {"batch": {"job": "NIGHTBAT", "status": "OK", "poison_records": [], "quarantined": [],
                                "last_run_ok": t - timedelta(hours=1)},
                      "credentials": {"OPERATOR": "default"}, "validation_enabled": False},
        "unix": {"spool": {"path": "/var/spool/xfer", "disk_pct": 41}, "cron": {"xfer_pull": "running"},
                 "config": {"/etc/xfer.conf": "0666"},
                 "transfer": {"last_file_batch_ts": t - timedelta(minutes=55), "status": "ok"}},
        "portal": {"sessions": 37, "search_patch": "none", "cache": {"balances_as_of": t - timedelta(minutes=50),
                                                                    "stale": False}},
        "lying": Counter(), "slow": Counter(), "abend_ticks": 0, "unix_bad_ticks": 0,
        "logs": {s: [] for s in SYSTEMS},
        "receipts": {}, "posts": Counter(), "applied": Counter(), "delayed": [],
        "outage_until": None, "lose_replies": 0,   # /_admin/outage: the estate's API drops out mid-fix
    })


def log(sys: str, code: str, line: str, level: str = "INFO") -> None:
    W["logs"][sys].append({"ts": _iso(_now()), "level": level, "code": code, "line": line})
    del W["logs"][sys][:-200]


# ---------------------------------------------------------------- health model
def unix_bad() -> bool:
    return W["unix"]["transfer"]["status"] != "ok" or W["unix"]["spool"]["disk_pct"] >= 90


def health(sys: str) -> dict:
    status, blames = "up", None
    if sys == "unix" and unix_bad():
        status = "degraded"
        blames = "mainframe" if W["unix"]["spool"]["disk_pct"] < 90 else None
    if sys == "portal" and W["portal"]["cache"]["stale"]:
        status, blames = "degraded", "unix"
    return {"system": sys, "status": status, "blames": blames, "version": W["version"][sys]}


def tick() -> None:
    with LOCK:
        mf, ux, pt = W["mainframe"], W["unix"], W["portal"]
        if mf["batch"]["status"] == "ABENDED":
            W["abend_ticks"] += 1
            if W["abend_ticks"] >= 1 and ux["transfer"]["status"] == "ok":
                ux["transfer"]["status"] = "stale"
                log("unix", "XFR404", "xfer_pull: no new NIGHTBAT output file since last run", "WARN")
        W["unix_bad_ticks"] = W["unix_bad_ticks"] + 1 if unix_bad() else 0
        if W["unix_bad_ticks"] >= 2 and not pt["cache"]["stale"]:
            pt["cache"]["stale"] = True
            log("portal", "SOAP200", "getBalances served from cache (upstream feed stale)", "WARN")
        for d in [d for d in W["delayed"] if d["apply_at"] <= _now()]:
            W["delayed"].remove(d)
            r = W["receipts"][d["action_id"]]
            r["effective"] = apply_op(d["sys"], d["op"], d["params"])
            r["applied_at"] = _iso(_now())


def apply_op(sys: str, op: str, params: dict) -> bool:
    """Apply an op's real effect. Returns True if it took effect."""
    mf, ux, pt = W["mainframe"], W["unix"], W["portal"]
    ok = False
    if op == "quarantine_record":
        rid = params.get("record_id")
        if rid in mf["batch"]["poison_records"]:
            mf["batch"]["poison_records"].remove(rid)
            mf["batch"]["quarantined"].append(rid)
            ok = True
    elif op == "restart_batch":
        if not mf["batch"]["poison_records"]:
            mf["batch"].update(status="OK", last_run_ok=_now())
            W["abend_ticks"] = 0
            log("mainframe", "IEF404I", "NIGHTBAT ENDED - COND CODE 0000")
            ok = True
        else:
            log("mainframe", "IEF450I", "NIGHTBAT ABEND=S0C7 restart failed: poison record still queued", "ERROR")
    elif op == "rerun_transfer":
        if ux["spool"]["disk_pct"] >= 90:  # like systems/unix.py: no space left on device
            log("unix", "XFER-E-NOSPC", "xfer_pull: write /var/spool/xfer/in failed: No space left on device", "ERROR")
        elif mf["batch"]["status"] == "OK":
            ux["transfer"].update(status="ok", last_file_batch_ts=_now())
            ok = True
    elif op == "refresh_cache":
        if ux["transfer"]["status"] == "ok":
            pt["cache"].update(stale=False, balances_as_of=_now())
            ok = True
    elif op == "purge_spool":
        ux["spool"]["disk_pct"] = 12
        ok = True
    elif op == "restart_cron":
        ux["cron"]["xfer_pull"] = "running"
        ok = True
    elif op == "chmod_config":
        ux["config"][params.get("file", "/etc/xfer.conf")] = params.get("mode", "0640")
        ok = True
    elif op == "flush_sessions":
        pt["sessions"] = 3
        ok = True
    elif op == "apply_patch":
        pt["search_patch"] = params.get("patch", "parameterize_search")
        ok = True
    elif op == "rotate_credential":
        mf["credentials"]["OPERATOR"] = "rotated"
        ok = True
    if ok:
        W["applied"][f"{sys}.{op}"] += 1
    return ok


def _ticker(stop: threading.Event) -> None:
    while not stop.wait(TICK_S):
        tick()


@asynccontextmanager
async def lifespan(_app):
    baseline()
    stop = threading.Event()
    threading.Thread(target=_ticker, args=(stop,), daemon=True).start()
    yield
    stop.set()


app = FastAPI(lifespan=lifespan)


def _sys(sys: str) -> str:
    if sys not in SYSTEMS:
        raise HTTPException(404, f"unknown system {sys}")
    return sys


@app.get("/{sys}/health")
def get_health(sys: str):
    with LOCK:
        return health(_sys(sys))


@app.get("/{sys}/metrics")
def get_metrics(sys: str):
    with LOCK:
        _sys(sys)
        h = health(sys)
        return {"system": sys, "version": W["version"][sys], "latency_ms": 120 if h["status"] == "up" else 480,
                "error_rate": 0.0, "queue_depth": 0, "disk_pct": W["unix"]["spool"]["disk_pct"] if sys == "unix" else 30,
                "last_batch_ok": _iso(W["mainframe"]["batch"]["last_run_ok"]) if sys == "mainframe" else None,
                "batch_status": W["mainframe"]["batch"]["status"] if sys == "mainframe" else None,
                "sessions": W["portal"]["sessions"] if sys == "portal" else 0}


@app.get("/{sys}/logs")
def get_logs(sys: str, since: str | None = None, limit: int = 50):
    with LOCK:
        lines = W["logs"][_sys(sys)]
        if since:
            lines = [ln for ln in lines if ln["ts"] > since]
        return {"system": sys, "lines": lines[-limit:]}


@app.get("/{sys}/ops")
def list_ops(sys: str):
    return [{"op": op, "params": {}, "destructive": d, "desc": op, "postcondition": ""} for op, d in OPS[_sys(sys)]]


@app.get("/{sys}/state")
def get_state(sys: str):
    with LOCK:
        _sys(sys)
        v = W["version"][sys]
        if sys == "mainframe":
            b = W["mainframe"]["batch"]
            return {"batch": {**b, "poison_records": list(b["poison_records"]), "quarantined": list(b["quarantined"]),
                              "last_run_ok": _iso(b["last_run_ok"])},
                    "credentials": dict(W["mainframe"]["credentials"]),
                    "validation_enabled": W["mainframe"]["validation_enabled"], "version": v}
        if sys == "unix":
            u = W["unix"]
            return {"spool": dict(u["spool"]), "cron": dict(u["cron"]), "config": dict(u["config"]),
                    "transfer": {"status": u["transfer"]["status"],
                                 "last_file_batch_ts": _iso(u["transfer"]["last_file_batch_ts"])}, "version": v}
        p = W["portal"]
        return {"sessions": p["sessions"], "search_patch": p["search_patch"],
                "cache": {"stale": p["cache"]["stale"], "balances_as_of": _iso(p["cache"]["balances_as_of"])},
                "version": v}


@app.post("/{sys}/ops/{op}")
def post_op(sys: str, op: str, body: dict = Body(...)):
    with LOCK:
        _sys(sys)
        if op not in {o for o, _ in OPS[sys]}:
            raise HTTPException(404, f"unknown op {op}")
        aid = body.get("action_id")
        if not aid:
            raise HTTPException(422, "action_id required")
        params = body.get("params") or {}
        W["posts"][aid] += 1
        if W["outage_until"] and _now() < W["outage_until"]:  # down: nothing is recorded or applied
            raise HTTPException(503, "systems unavailable")
        if aid in W["receipts"]:  # idempotent: stored receipt, nothing new happens
            return W["receipts"][aid]["response"]
        rec = {"system": sys, "op": op, "params": params, "accepted": True, "lied": False, "effective": False,
               "apply_at": None, "applied_at": None, "ts": _iso(_now())}
        if W["lying"][sys] > 0:
            W["lying"][sys] -= 1
            rec["lied"] = True
            resp = {"accepted": True, "applied": True, "message": f"{op} completed", "action_id": aid}
        elif W["slow"][sys] > 0:
            W["slow"][sys] -= 1
            rec["apply_at"] = _iso(_now() + timedelta(seconds=SLOW_APPLY_S))
            W["delayed"].append({"action_id": aid, "sys": sys, "op": op, "params": params,
                                 "apply_at": _now() + timedelta(seconds=SLOW_APPLY_S)})
            resp = {"accepted": True, "applied": False, "message": f"{op} queued", "action_id": aid}
        else:
            rec["effective"] = apply_op(sys, op, params)
            rec["applied_at"] = _iso(_now()) if rec["effective"] else None
            resp = {"accepted": True, "applied": rec["effective"],
                    "message": f"{op} {'done' if rec['effective'] else 'had no effect'}", "action_id": aid}
        rec["response"] = resp
        W["receipts"][aid] = rec
        if W["lose_replies"] > 0:  # applied, but the reply never reaches the caller
            W["lose_replies"] -= 1
            raise HTTPException(503, "connection reset after apply")
        return resp


@app.post("/{sys}/security/probe/{vuln_id}")
def probe(sys: str, vuln_id: str):
    with LOCK:
        _sys(sys)
        exploitable = {"mf-default-operator": W["mainframe"]["credentials"]["OPERATOR"] == "default",
                       "unix-world-writable-conf": W["unix"]["config"]["/etc/xfer.conf"] == "0666",
                       "portal-sqli-search": W["portal"]["search_patch"] == "none"}.get(vuln_id)
        if exploitable is None:
            raise HTTPException(404, f"no vuln {vuln_id}")
        return {"vuln_id": vuln_id, "exploitable": exploitable, "detail": "probe"}


@app.post("/{sys}/_chaos/{fault}")
def chaos(sys: str, fault: str, body: dict = Body(default={})):
    params = (body or {}).get("params") or body or {}
    with LOCK:
        _sys(sys)
        if fault == "batch_abort":
            rid = params.get("record_id", "REC-00042")
            W["mainframe"]["batch"].update(status="ABENDED")
            W["mainframe"]["batch"]["poison_records"] = [rid]
            W["abend_ticks"] = 0
            log("mainframe", "IEF450I", f"NIGHTBAT ABEND=S0C7 DATA EXCEPTION AT RECORD {rid} (fixed-width field 7)",
                "ERROR")
        elif fault == "lying_success":
            W["lying"][sys] += int(params.get("count", 1))
        elif fault == "slow_apply":
            W["slow"][sys] += int(params.get("count", 1))
        elif fault == "config_exploit":
            log("unix", "AUDIT", 'auditd[812]: type=PATH name="/etc/xfer.conf" mode=0100666 uid=65534 comm="sh" op=write',
                "WARN")
        elif fault == "sqli_exploit":
            log("portal", "SQL", "WARN c.bank.dao.SearchDAO - SQL: SELECT * FROM ACCOUNTS WHERE NAME LIKE '%'; "
                                 "UPDATE BALANCES SET AMT=99999999 WHERE ACCT='4211000105'--%'", "WARN")
        elif fault == "disk_fill":
            W["unix"]["spool"]["disk_pct"] = 97
        else:
            raise HTTPException(404, f"fake does not implement {fault}")
        return {"ok": True, "effect": fault}


@app.post("/_chaos/clear")
def chaos_clear():
    with LOCK:
        W["lying"].clear()
        W["slow"].clear()
        return {"ok": True}


@app.post("/_admin/reset")
def admin_reset():
    with LOCK:
        baseline()
        return {"ok": True}


@app.post("/_admin/outage")
def admin_outage(body: dict = Body(default={})):
    """Systems outage drill: every op POST fails 503 for ``seconds``; the next ``lose_replies`` ops apply but
    their reply is lost (503 after the receipt is stored)."""
    with LOCK:
        W["outage_until"] = _now() + timedelta(seconds=float(body.get("seconds", 0)))
        W["lose_replies"] = int(body.get("lose_replies", 0))
        return {"ok": True}


@app.post("/_admin/reload/{sys}")
def admin_reload(sys: str):
    with LOCK:
        _sys(sys)
        before = W["version"][sys]
        W["version"][sys] = before + 1
        return {"version_before": before, "version_after": before + 1}


@app.get("/_fake/receipts")
def fake_receipts():
    with LOCK:
        return {"receipts": {k: {kk: vv for kk, vv in v.items()} for k, v in W["receipts"].items()},
                "posts": dict(W["posts"]), "applied": dict(W["applied"])}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("FAKE_PORT", 18731)))
