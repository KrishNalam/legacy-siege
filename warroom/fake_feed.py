"""Scripted dev feed for the war room: plays the bank-night story straight into MongoDB.

    WORLD_DB=world_dev SWARM_DB=swarm_dev .venv/bin/python -m warroom.fake_feed [--speed 1.0] [--until 60]

It writes the same documents (shapes from CONTRACTS.md) the real systems / chaos / swarm /
gardener processes would write, so the war room can be developed, screenshotted and rehearsed
without the rest of the stack:

  night 1  lying_success + batch_abort → cascade (mainframe → unix → portal) → incident → recall miss
           → diagnosis walk portal → unix → mainframe → plan → Guard catches the lie (ACK ≠ APPLIED)
           → Beekeeper approval → agent_kill mid-step → Undertaker → Nurse resumes from checkpoint
           → resolve → playbook (honey)
  night 2  same fault class → $vectorSearch recall hit → chain walk skipped → resolve fast
  gardener recurring fault → upgrade request → tests → approval → deploy v2 → night 3 replay → proven

Defaults to world_dev / swarm_dev (refuses to touch the real 'world'/'swarm' unless --force).
Step transitions use a multi-document transaction (incident step + ledger receipt), exactly like Blue.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import timedelta

os.environ.setdefault("WORLD_DB", "world_dev")
os.environ.setdefault("SWARM_DB", "swarm_dev")

from common import config  # noqa: E402
from common.db import client, now, swarm, world  # noqa: E402
from common.embed import embed, model_id  # noqa: E402

W, S = world, swarm
STOP = threading.Event()
ROSTER = [
    ("queen-1", "queen", None), ("queen-2", "queen", None), ("scout-1", "scout", None), ("scout-2", "scout", None),
    ("worker-mainframe-1", "worker", "mainframe"), ("worker-unix-1", "worker", "unix"), ("worker-web-1", "worker", "web"),
    ("guard-1", "guard", None), ("waggle-1", "waggle", None), ("sentinel-1", "sentinel", None),
    ("nurse-1", "nurse", None), ("nurse-2", "nurse", None), ("gardener-1", "gardener", None),
]
BASE_METRICS = {
    "mainframe": {"latency_ms": 38, "error_rate": 0.0, "queue_depth": 3, "disk_pct": 61, "sessions": 0, "batch_status": "OK"},
    "unix": {"latency_ms": 22, "error_rate": 0.0, "queue_depth": 0, "disk_pct": 43, "sessions": 0, "batch_status": None},
    "portal": {"latency_ms": 140, "error_rate": 0.002, "queue_depth": 0, "disk_pct": 35, "sessions": 412, "batch_status": None},
}
VULNS = {
    "mainframe": [{"id": "MF-CRED-01", "kind": "default_credential", "desc": "OPERATOR account uses default password", "open": True, "exploited": False}],
    "unix": [{"id": "UX-CONF-01", "kind": "world_writable_config", "desc": "/etc/xfer.conf is 0666", "open": True, "exploited": False}],
    "portal": [{"id": "WP-SQLI-01", "kind": "sqli", "desc": "account search concatenates SQL", "open": True, "exploited": False}],
}
DISPLAY = {"mainframe": "Mainframe (z/OS)", "unix": "Unix middleware (AIX)", "portal": "Web portal"}


# ------------------------------------------------------------------ tiny helpers
def think(agent, role, text, incident=None, system=None, kind="thought"):
    S().thoughts.insert_one({"agent": agent, "role": role, "incident": incident, "system": system,
                             "text": text[:280], "kind": kind, "ts": now()})


def agent(aid, **fields):
    S().agents.update_one({"_id": aid}, {"$set": {**fields, "last_seen": now()}})


def system(sid, metrics=None, **fields):
    upd = {**fields, "updated_at": now()}
    for k, v in (metrics or {}).items():
        upd[f"metrics.{k}"] = v
    W().systems.update_one({"_id": sid}, {"$set": upd})


def log(sid, level, code, line):
    W().system_logs.insert_one({"system": sid, "ts": now(), "level": level, "code": code, "line": line})


def chaos(cid, fault, target, params=None, run=None, result=None, delay=0.6):
    """Insert as 'firing' (lightning on screen), then land the outcome — like chaos.runner.fire."""
    W().chaos_events.insert_one({"_id": cid, "fault": fault, "target": target, "params": params or {}, "ts": now(),
                                 "scenario": run and run["scenario"], "run_id": run and run["_id"],
                                 "round": run and run["round"], "seed": run and run.get("seed"),
                                 "manual": run is None, "status": "firing", "result": None})
    def land():
        time.sleep(delay)
        W().chaos_events.update_one({"_id": cid}, {"$set": {"status": "ok", "result": result or {"ok": True}, "done_at": now()}})
    threading.Thread(target=land, daemon=True).start()


def start_run(rid, scenario, rnd, seed):
    run = {"_id": rid, "scenario": scenario, "round": rnd, "seed": seed, "status": "running", "started_at": now(), "ended_at": None}
    W().runs.update_many({"ended_at": None}, {"$set": {"ended_at": now()}})
    W().runs.insert_one(run)
    S().stats.update_one({"_id": "current"}, {"$set": {"run_id": rid, "round": rnd, "scenario": scenario, "ts": now()}}, upsert=True)
    return run


def step_txn(inc_id, step_id, step_set: dict, ledger_id: str, ledger_set: dict, add_attempt: str | None = None):
    """One step transition = one transaction: incident step + that attempt's ledger receipt."""
    def body(session):
        upd = {"$set": {f"steps.$.{k}": v for k, v in step_set.items()}}
        if add_attempt:
            upd["$addToSet"] = {"steps.$.attempts": add_attempt}
        S().incidents.update_one({"_id": inc_id, "steps.step_id": step_id}, upd, session=session)
        S().ledger.update_one({"action_id": ledger_id},
                              {"$set": {**ledger_set, "updated_at": now()},
                               "$setOnInsert": {"action_id": ledger_id, "step_id": step_id, "incident": inc_id, "ts": now()}},
                              upsert=True, session=session)
    with client().start_session() as s:
        s.with_transaction(body)


def send(inc, sid, n, agent_id, op, system_id, attempt=1, params=None):
    aid = f"{sid}-a{attempt}"
    step_txn(inc, sid, {"status": "sent", "action_id": aid, "agent": agent_id, "ts": now()}, aid,
             {"agent": agent_id, "system": system_id, "op": op, "params": params or {}, "status": "sent",
              "system_response": {"accepted": True, "applied": True}}, add_attempt=aid)


def verify(inc, sid, attempt, evidence, guard="guard-1"):
    aid = f"{sid}-a{attempt}"
    step_txn(inc, sid, {"status": "verified", "evidence": evidence, "ts": now()}, aid,
             {"agent": guard, "op": "verify", "status": "verified", "evidence": evidence})


def wait_approval(apr_id, auto_after):
    """Give a human ~auto_after seconds to click Approve in the war room, then approve as 'auto'."""
    deadline = time.monotonic() + auto_after
    while time.monotonic() < deadline and not STOP.is_set():
        doc = S().approvals.find_one({"_id": apr_id})
        if doc and doc["status"] != "pending":
            return doc["status"]
        time.sleep(0.2)
    S().approvals.update_one({"_id": apr_id, "status": "pending"},
                             {"$set": {"status": "approved", "decided_by": "fake_feed(auto)", "decided_at": now()}})
    return "approved"


# ------------------------------------------------------------------ background "life"
def heartbeats():
    """Agents heartbeat every HEARTBEAT_S (the war room aggregates these, doesn't animate them)."""
    while not STOP.wait(config.HEARTBEAT_S):
        S().agents.update_many({"status": {"$ne": "dead"}}, {"$set": {"last_seen": now()}})


def system_ticks():
    """The systems' 1 s tick: jittery metrics, like world.systems under A's tick loop."""
    import random
    r = random.Random(7)
    while not STOP.wait(1.0):
        for sid in ("mainframe", "unix", "portal"):
            doc = W().systems.find_one({"_id": sid}, {"status": 1, "metrics": 1})
            if not doc:
                continue
            base = BASE_METRICS[sid]["latency_ms"] * (3.5 if doc.get("status") != "up" else 1)
            W().systems.update_one({"_id": sid}, {"$set": {"metrics.latency_ms": round(base * (0.85 + 0.3 * r.random())),
                                                           "updated_at": now()}})


# ------------------------------------------------------------------ the story
def baseline():
    from scripts.setup_db import reset
    reset()
    W().counters.delete_many({})
    t = now()
    W().systems.insert_many([{
        "_id": sid, "display": DISPLAY[sid], "version": 1, "status": "up", "blames": None,
        "depends_on": [d] if d else [], "metrics": {**BASE_METRICS[sid], "last_batch_ok": t - timedelta(hours=2)},
        "vulns": VULNS[sid], "faults_active": [], "symptom": None, "updated_at": t,
    } for sid, d in (("mainframe", None), ("unix", "mainframe"), ("portal", "unix"))])
    for sid, line in (("mainframe", "NIGHTBAT scheduled 02:00 · CICS online region ACTIVE"),
                      ("unix", "xfer_pull: waiting for NIGHTBAT.OUT"), ("portal", "GET /balances 200 (cache as_of 02:07)")):
        log(sid, "INFO", "", line)
    S().agents.insert_many([{
        "_id": aid, "role": role, "specialty": spec, "status": "standby" if role == "nurse" or aid == "queen-2" else "idle",
        "target": "bench" if role == "nurse" else "workshop" if role == "gardener" else None,
        "incident": None, "task": None, "phase": None, "pid": 40000 + i, "last_seen": t, "generation": 1,
        "started_at": t, "standby_for": "queen-1" if aid == "queen-2" else None,
    } for i, (aid, role, spec) in enumerate(ROSTER)])


def night1(T):
    run = start_run("RUN-0001", "bank-night-1", 1, 1001)
    T(1)
    chaos("CHX-0001", "lying_success", "mainframe", {"count": 1}, run)
    system("mainframe", faults_active=[{"fault": "lying_success", "since": now(), "params": {"count": 1}}])
    T(2.5)
    chaos("CHX-0002", "batch_abort", "mainframe", {}, run)
    system("mainframe", {"batch_status": "ABENDED"},
           faults_active=[{"fault": "lying_success", "since": now(), "params": {"count": 1}},
                          {"fault": "batch_abort", "since": now(), "params": {}}])
    log("mainframe", "ERROR", "IEF450I", "NIGHTBAT ABEND=S0C7 RECORD R-88412")
    T(5)
    system("unix", {"error_rate": 0.04, "queue_depth": 17}, status="degraded", blames="mainframe",
           symptom="no batch file from mainframe since 02:00")
    log("unix", "WARN", "XFR201W", "no inbound file NIGHTBAT.OUT after 02:30 window")
    T(7.5)
    system("portal", {"error_rate": 0.0}, status="degraded", blames="unix",
           symptom="serving stale balances (HTTP 200)")
    log("portal", "WARN", "CACHE", "balances cache as_of 02:07 (26h old) · still 200 OK")
    T(8.5)
    inc = "INC-0001"
    think("sentinel-1", "sentinel", "Portal /health says 200 but balances are 26h old; unix degraded too.", inc, "portal")
    S().incidents.insert_one({
        "_id": inc, "active": True, "correlation_key": "chain:portal", "status": "open", "system": "portal",
        "affected": ["portal", "unix"], "symptoms": ["portal serving stale balances (HTTP 200)", "unix: no batch file"],
        "owner": None, "generation": 0, "run_id": run["_id"], "round": 1, "diagnosis_path": [], "handoffs": [],
        "steps": [], "opened_at": now(), "metrics": {}})
    T(10)
    S().incidents.update_one({"_id": inc}, {"$set": {"owner": "scout-1", "status": "claimed", "lease_until": now() + timedelta(seconds=10)},
                                            "$inc": {"generation": 1}})
    agent("scout-1", status="working", target="portal", incident=inc, phase="recall")
    think("scout-1", "scout", "Claimed INC-0001 with an atomic lease. Checking the honey first.", inc, "portal", "action")
    T(11.5)
    q = "portal serving stale balances HTTP 200; unix no batch file from mainframe"
    S().incidents.update_one({"_id": inc}, {"$set": {"recall": {
        "query": q, "filter": {"embedding_model": model_id(), "stale": False}, "hits": [], "used": False, "model": model_id()}}})
    think("scout-1", "scout", "$vectorSearch: 0 playbooks. First time we see this. Walking the chain.", inc, None, "recall")
    T(13)
    S().tasks.insert_one({"_id": f"{inc}-t1", "incident": inc, "specialty": "web", "kind": "diagnose", "step_ids": [],
                          "status": "claimed", "owner": "worker-web-1", "lease_until": now() + timedelta(seconds=10),
                          "generation": 1, "checkpoint_thread": f"{inc}-t1", "ts": now()})
    S().incidents.update_one({"_id": inc}, {"$set": {"status": "diagnosing", "diagnosis_path": ["portal"]}})
    agent("worker-web-1", status="working", target="portal", incident=inc, task=f"{inc}-t1", phase="diagnosing")
    agent("scout-1", phase="diagnosing")
    think("worker-web-1", "worker", "Portal cache as_of 02:07 and /health blames unix.", inc, "portal")
    T(16)
    S().tasks.insert_one({"_id": f"{inc}-t2", "incident": inc, "specialty": "unix", "kind": "diagnose", "step_ids": [],
                          "status": "claimed", "owner": "worker-unix-1", "lease_until": now() + timedelta(seconds=10),
                          "generation": 1, "checkpoint_thread": f"{inc}-t2", "ts": now()})
    S().tasks.update_one({"_id": f"{inc}-t1"}, {"$set": {"status": "done"}})
    S().incidents.update_one({"_id": inc}, {"$push": {"diagnosis_path": "unix"}})
    agent("worker-unix-1", status="working", target="unix", incident=inc, task=f"{inc}-t2", phase="diagnosing")
    agent("worker-web-1", phase=None, task=None)
    agent("scout-1", target="unix")
    think("worker-unix-1", "worker", "Spool is empty: transfer is waiting on NIGHTBAT.OUT. Unix blames mainframe.", inc, "unix")
    T(19)
    S().tasks.insert_one({"_id": f"{inc}-t3", "incident": inc, "specialty": "mainframe", "kind": "fix", "step_ids": [],
                          "status": "claimed", "owner": "worker-mainframe-1", "lease_until": now() + timedelta(seconds=10),
                          "generation": 1, "checkpoint_thread": f"{inc}-t3", "ts": now()})
    S().tasks.update_one({"_id": f"{inc}-t2"}, {"$set": {"status": "done"}})
    S().incidents.update_one({"_id": inc}, {"$push": {"diagnosis_path": "mainframe"},
                                            "$set": {"metrics.diagnosis_s": 9.0, "metrics.hops": 3}})
    agent("worker-mainframe-1", status="working", target="mainframe", incident=inc, task=f"{inc}-t3", phase="diagnosing")
    agent("worker-unix-1", phase=None, task=None)
    agent("scout-1", target="mainframe")
    think("worker-mainframe-1", "worker", "Mainframe /health is 'up' but the log says IEF450I NIGHTBAT ABEND=S0C7 on record R-88412.", inc, "mainframe")
    T(20.5)
    S().incidents.update_one({"_id": inc}, {"$set": {
        "root_cause_system": "mainframe", "fault_class": "batch_abort",
        "root_cause": "NIGHTBAT abended on malformed record R-88412 (S0C7)"}})
    T(22)
    steps = [
        {"step_id": f"{inc}-s1", "n": 1, "desc": "Quarantine poison record", "system": "mainframe", "op": "quarantine_record",
         "params": {"record_id": "R-88412"}, "destructive": False},
        {"step_id": f"{inc}-s2", "n": 2, "desc": "Restart NIGHTBAT", "system": "mainframe", "op": "restart_batch",
         "params": {"job": "NIGHTBAT"}, "destructive": True},
        {"step_id": f"{inc}-s3", "n": 3, "desc": "Re-run file transfer", "system": "unix", "op": "rerun_transfer", "params": {}, "destructive": False},
        {"step_id": f"{inc}-s4", "n": 4, "desc": "Refresh balances cache", "system": "portal", "op": "refresh_cache", "params": {}, "destructive": False},
    ]
    for s in steps:
        s.update({"status": "planned", "attempts": [], "task_id": f"{inc}-t3" if s["system"] == "mainframe" else None,
                  "agent": None, "evidence": None, "action_id": None, "ts": now()})
    S().incidents.update_one({"_id": inc}, {"$set": {"steps": steps, "status": "fixing"}})
    agent("scout-1", status="idle", target=None, phase=None)
    think("queen-1", "queen", "Plan: quarantine R-88412 → restart NIGHTBAT (needs approval) → rerun transfer → refresh cache.", inc, None, "action")
    T(23.5)
    send(inc, f"{inc}-s1", 1, "worker-mainframe-1", "quarantine_record", "mainframe", 1, {"record_id": "R-88412"})
    agent("worker-mainframe-1", phase="step_sent")
    W().op_receipts.insert_one({"_id": f"{inc}-s1-a1", "system": "mainframe", "op": "quarantine_record", "accepted": True,
                                "claimed_applied": True, "effective": False, "lied": True, "ts": now()})
    think("worker-mainframe-1", "worker", "quarantine_record sent. Mainframe says: accepted, applied.", inc, "mainframe", "action")
    T(25)
    agent("guard-1", status="working", target="mainframe", incident=inc, phase="verifying")
    agent("worker-mainframe-1", phase="awaiting_verify")
    think("guard-1", "guard", "Reading back /state for mainframe…", inc, "mainframe", "verify")
    T(27)
    think("guard-1", "guard", "ACK ≠ APPLIED: R-88412 is still in poison_records, quarantined=[]. Rejecting attempt a1.", inc, "mainframe", "reject")
    step_txn(inc, f"{inc}-s1", {"status": "failed"}, f"{inc}-s1-a1", {"status": "failed", "evidence": "poison_records still contains R-88412"})
    system("mainframe", faults_active=[{"fault": "batch_abort", "since": now(), "params": {}}])
    T(29)
    send(inc, f"{inc}-s1", 1, "worker-mainframe-1", "quarantine_record", "mainframe", 2, {"record_id": "R-88412"})
    agent("worker-mainframe-1", phase="step_sent")
    think("worker-mainframe-1", "worker", "Retrying as a NEW attempt (INC-0001-s1-a2), never a blind re-send.", inc, "mainframe", "action")
    T(31)
    verify(inc, f"{inc}-s1", 2, "batch.quarantined=['R-88412'], poison_records=[]")
    think("guard-1", "guard", "Verified s1: R-88412 quarantined (read-back).", inc, "mainframe", "verify")
    agent("guard-1", status="idle", target=None, phase=None)
    T(32.5)
    S().approvals.insert_one({"_id": "APR-0001", "kind": "step", "incident": inc, "step_id": f"{inc}-s2", "action_id": f"{inc}-s2-a1",
                              "requested_by": "queen-1", "desc": "Restart NIGHTBAT on mainframe (destructive)",
                              "risk": "re-runs the nightly batch; CICS contention for ~4 min", "status": "pending", "ts": now()})
    S().incidents.update_one({"_id": inc, "steps.step_id": f"{inc}-s2"}, {"$set": {"status": "awaiting_approval", "steps.$.approval_id": "APR-0001"}})
    agent("queen-1", status="working", phase="awaiting_approval", incident=inc)
    agent("worker-mainframe-1", phase="awaiting_approval")
    think("queen-1", "queen", "restart_batch is destructive. Asking the Beekeeper before anything is sent.", inc, "mainframe", "approval")
    t_req = time.monotonic()
    wait_approval("APR-0001", T.scale(5))
    wait_s = time.monotonic() - t_req
    T.rebase()
    S().incidents.update_one({"_id": inc}, {"$set": {"status": "fixing", "metrics.approval_wait_s": round(wait_s, 1)}})
    agent("queen-1", status="idle", phase=None)
    think("queen-1", "queen", "Beekeeper approved APR-0001. Go.", inc, "mainframe", "approval")
    T(1)
    send(inc, f"{inc}-s2", 2, "worker-mainframe-1", "restart_batch", "mainframe", 1, {"job": "NIGHTBAT"})
    agent("worker-mainframe-1", phase="step_sent")
    cp = "1efa3c9e-7b21-6d40-8002-5c1f0e2d9a7b"
    S().checkpoints.insert_one({"thread_id": f"{inc}-t3", "checkpoint_ns": "", "checkpoint_id": cp, "type": "msgpack", "checkpoint": b"\x00" * 64})
    T(2.5)
    chaos("CHX-0003", "agent_kill", "swarm", {"selector": {"role": "worker", "phase": "step_sent"}}, run,
          result={"ok": True, "agent": "worker-mainframe-1", "pid": 40004, "signal": "SIGKILL", "task": f"{inc}-t3"}, delay=1.2)
    T(3.7)
    agent("worker-mainframe-1", status="dead", died_at=now())
    system("mainframe", {"batch_status": "OK", "last_batch_ok": now()}, status="up", faults_active=[], symptom=None)
    log("mainframe", "INFO", "IEF404I", "NIGHTBAT ENDED RC=0000 (restart)")
    T(8)
    think("sentinel-1", "sentinel", "Undertaker: worker-mainframe-1 missed 3 heartbeats. Releasing lease on INC-0001-t3.", inc, "mainframe", "handoff")
    S().tasks.update_one({"_id": f"{inc}-t3"}, {"$set": {"owner": None, "lease_until": now() - timedelta(seconds=1)}})
    S().messages.insert_one({"from": "waggle-1", "to": "all", "kind": "handoff", "body": {"task": f"{inc}-t3", "dead": "worker-mainframe-1"}, "ts": now()})
    think("waggle-1", "waggle", "Handoff: INC-0001-t3 is up for grabs (worker-mainframe-1 down).", inc, None, "handoff")
    T(10)
    S().tasks.update_one({"_id": f"{inc}-t3"}, {"$set": {"owner": "nurse-1", "lease_until": now() + timedelta(seconds=10)}, "$inc": {"generation": 1}})
    agent("nurse-1", status="working", target="mainframe", incident=inc, task=f"{inc}-t3", phase="reconciling", generation=2)
    S().incidents.update_one({"_id": inc}, {"$push": {"handoffs": {
        "from": "worker-mainframe-1", "to": "nurse-1", "ts": now(), "reason": "lease expired", "checkpoint_id": cp,
        "skipped_steps": [f"{inc}-s1"]}}, "$set": {"metrics.takeover_s": 6.3}})
    think("nurse-1", "nurse", f"Resumed INC-0001-t3 from checkpoint {cp[:13]}. s1 already verified; s2 was 'sent', reconciling with /state.", inc, "mainframe", "handoff")
    T(13)
    think("nurse-1", "nurse", "restart_batch already applied (batch.status=OK). Not re-sending; handing to the Guard.", inc, "mainframe")
    agent("guard-1", status="working", target="mainframe", incident=inc, phase="verifying")
    T(14.5)
    verify(inc, f"{inc}-s2", 1, "batch.status=OK, last_run_ok fresh (02:51:07Z)")
    think("guard-1", "guard", "Verified s2: NIGHTBAT RC=0000, last_run_ok fresh.", inc, "mainframe", "verify")
    agent("nurse-1", phase=None, status="standby", target="bench", task=None)
    agent("guard-1", status="idle", target=None, phase=None)
    T(16)
    agent("worker-unix-1", status="working", target="unix", incident=inc, task=f"{inc}-t4", phase="step_sent")
    send(inc, f"{inc}-s3", 3, "worker-unix-1", "rerun_transfer", "unix", 1)
    T(18)
    verify(inc, f"{inc}-s3", 1, "transfer.last_file_batch_ts >= mainframe.last_run_ok")
    system("unix", {"error_rate": 0.0, "queue_depth": 0}, status="up", blames=None, symptom=None)
    log("unix", "INFO", "XFR100I", "NIGHTBAT.OUT received, 1,204,551 records")
    agent("worker-unix-1", status="idle", target=None, task=None, phase=None)
    agent("worker-mainframe-1", status="idle", target=None, task=None, phase=None, generation=2, died_at=None)  # supervisor respawn
    T(20)
    agent("worker-web-1", status="working", target="portal", incident=inc, task=f"{inc}-t5", phase="step_sent")
    send(inc, f"{inc}-s4", 4, "worker-web-1", "refresh_cache", "portal", 1)
    T(22)
    verify(inc, f"{inc}-s4", 1, "cache.stale=false, balances_as_of=now")
    system("portal", status="up", blames=None, symptom=None)
    log("portal", "INFO", "CACHE", "balances refreshed as_of 02:53")
    T(23.5)
    resolve(inc, steps=4, attempts=5, hops=3, symptom="portal serving stale balances HTTP 200; unix no batch file from mainframe")
    agent("worker-web-1", status="idle", target=None, task=None, phase=None)


def resolve(inc, steps, attempts, hops, symptom):
    doc = S().incidents.find_one({"_id": inc})
    closed = now()
    ttr = (closed - doc["opened_at"]).total_seconds()
    S().incidents.update_one({"_id": inc}, {"$set": {
        "status": "resolved", "active": False, "closed_at": closed, "owner": None,
        "metrics.ttr_s": round(ttr, 1), "metrics.steps": steps, "metrics.attempts": attempts, "metrics.hops": hops}})
    n = S().playbooks.count_documents({}) + 1
    S().playbooks.insert_one({
        "_id": f"PB-{n:04d}", "incident": inc, "system": "mainframe", "symptom_system": "portal", "system_version": 1,
        "fault_class": "batch_abort", "symptoms": doc.get("symptoms", []), "symptom_text": symptom,
        "root_cause": "NIGHTBAT abended on a malformed record (S0C7)",
        "fix": [{"system": "mainframe", "op": "quarantine_record"}, {"system": "mainframe", "op": "restart_batch"},
                {"system": "unix", "op": "rerun_transfer"}, {"system": "portal", "op": "refresh_cache"}],
        "outcome": "resolved", "metrics": {"ttr_s": round(ttr, 1)}, "embedding": embed(symptom),
        "embedding_model": model_id(), "stale": False, "created_at": now(), "verified_at": now()})
    think("queen-1", "queen", f"Resolved {inc} in {ttr:.0f}s. Saved PB-{n:04d} to the honey (embedded for $vectorSearch).", inc, None, "learn")


def night2(T):
    run = start_run("RUN-0002", "bank-night-2", 2, 1002)
    T(1)
    chaos("CHX-0004", "batch_abort", "mainframe", {}, run)
    system("mainframe", {"batch_status": "ABENDED"}, faults_active=[{"fault": "batch_abort", "since": now(), "params": {}}])
    log("mainframe", "ERROR", "IEF450I", "NIGHTBAT ABEND=S0C7 RECORD R-90117")
    T(3)
    system("unix", status="degraded", blames="mainframe", symptom="no batch file from mainframe since 02:00")
    T(4.5)
    system("portal", status="degraded", blames="unix", symptom="serving stale balances (HTTP 200)")
    T(5.5)
    inc = "INC-0002"
    S().incidents.insert_one({
        "_id": inc, "active": True, "correlation_key": "chain:portal", "status": "open", "system": "portal",
        "affected": ["portal", "unix"], "symptoms": ["portal serving stale balances (HTTP 200)", "unix: no batch file"],
        "owner": None, "generation": 0, "run_id": run["_id"], "round": 2, "diagnosis_path": [], "handoffs": [],
        "steps": [], "opened_at": now(), "metrics": {}})
    think("sentinel-1", "sentinel", "Same smell as last night: portal stale with HTTP 200.", inc, "portal")
    T(6.5)
    S().incidents.update_one({"_id": inc}, {"$set": {"owner": "scout-1", "status": "claimed"}, "$inc": {"generation": 1}})
    agent("scout-1", status="working", target="portal", incident=inc, phase="recall")
    T(7.5)
    q = "portal serving stale balances HTTP 200; unix no batch file from mainframe"
    S().incidents.update_one({"_id": inc}, {"$set": {
        "recall": {"query": q, "filter": {"embedding_model": model_id(), "stale": False}, "used": True, "model": model_id(),
                   "hits": [{"playbook_id": "PB-0001", "score": 0.9412, "stale": False, "fault_class": "batch_abort", "system_version": 1}]},
        "diagnosis_path": ["portal"], "root_cause_system": "mainframe", "fault_class": "batch_abort",
        "root_cause": "NIGHTBAT abended on a malformed record (S0C7) · from PB-0001", "metrics.diagnosis_s": 1.2, "metrics.hops": 0}})
    think("scout-1", "scout", "$vectorSearch hit PB-0001 (0.94), mainframe v1 matches. Skipping the chain walk.", inc, None, "recall")
    T(9)
    steps = []
    for n, (op, sys_, params, destr) in enumerate([
            ("quarantine_record", "mainframe", {"record_id": "R-90117"}, False), ("restart_batch", "mainframe", {"job": "NIGHTBAT"}, True),
            ("rerun_transfer", "unix", {}, False), ("refresh_cache", "portal", {}, False)], start=1):
        steps.append({"step_id": f"{inc}-s{n}", "n": n, "desc": op, "system": sys_, "op": op, "params": params, "destructive": destr,
                      "status": "planned", "attempts": [], "agent": None, "evidence": None, "action_id": None, "ts": now()})
    S().incidents.update_one({"_id": inc}, {"$set": {"steps": steps, "status": "fixing"}})
    agent("scout-1", status="idle", target=None, phase=None)
    agent("worker-mainframe-1", status="working", target="mainframe", incident=inc, task=f"{inc}-t1", phase="step_sent")
    think("queen-1", "queen", "Replaying PB-0001's verified fix (4 steps).", inc, None, "action")
    send(inc, f"{inc}-s1", 1, "worker-mainframe-1", "quarantine_record", "mainframe", 1)
    T(10.5)
    agent("guard-1", status="working", target="mainframe", phase="verifying")
    verify(inc, f"{inc}-s1", 1, "batch.quarantined=['R-90117'], poison_records=[]")
    S().approvals.insert_one({"_id": "APR-0002", "kind": "step", "incident": inc, "step_id": f"{inc}-s2", "requested_by": "queen-1",
                              "desc": "Restart NIGHTBAT on mainframe (destructive)", "risk": "re-runs the nightly batch",
                              "status": "pending", "ts": now()})
    S().incidents.update_one({"_id": inc}, {"$set": {"status": "awaiting_approval"}})
    t_req = time.monotonic()
    wait_approval("APR-0002", T.scale(3))
    T.rebase()
    S().incidents.update_one({"_id": inc}, {"$set": {"status": "fixing", "metrics.approval_wait_s": round(time.monotonic() - t_req, 1)}})
    send(inc, f"{inc}-s2", 2, "worker-mainframe-1", "restart_batch", "mainframe", 1)
    T(1.5)
    verify(inc, f"{inc}-s2", 1, "batch.status=OK, last_run_ok fresh")
    system("mainframe", {"batch_status": "OK", "last_batch_ok": now()}, status="up", faults_active=[], symptom=None)
    agent("worker-mainframe-1", status="idle", target=None, task=None, phase=None)
    agent("worker-unix-1", status="working", target="unix", incident=inc, phase="step_sent")
    send(inc, f"{inc}-s3", 3, "worker-unix-1", "rerun_transfer", "unix", 1)
    T(3)
    verify(inc, f"{inc}-s3", 1, "transfer.last_file_batch_ts >= mainframe.last_run_ok")
    system("unix", status="up", blames=None, symptom=None)
    agent("worker-unix-1", status="idle", target=None, phase=None)
    agent("worker-web-1", status="working", target="portal", incident=inc, phase="step_sent")
    send(inc, f"{inc}-s4", 4, "worker-web-1", "refresh_cache", "portal", 1)
    T(4.5)
    verify(inc, f"{inc}-s4", 1, "cache.stale=false, balances_as_of=now")
    system("portal", status="up", blames=None, symptom=None)
    agent("worker-web-1", status="idle", target=None, phase=None)
    agent("guard-1", status="idle", target=None, phase=None)
    T(5.5)
    resolve(inc, steps=4, attempts=4, hops=0, symptom=q)


DIFF = """--- a/systems/mainframe.py
+++ b/systems/mainframe.py
@@ -88,6 +88,14 @@ def run_nightbat(state):
+    # Gardener: validate records before the batch runs (batch_abort seen 2x)
+    bad = [r for r in state.records if not valid_packed_decimal(r)]
+    for r in bad:
+        state.quarantine(r.id, reason="failed validation")
+    if bad:
+        log("IEF196I", f"{len(bad)} record(s) quarantined pre-run")
     for record in state.records:
-        post(record)
+        post(record)  # only validated records reach here"""


def gardener(T):
    upg = "UPG-0001"
    S().upgrade_requests.insert_one({"_id": upg, "system": "mainframe", "weakness": "no input validation in NIGHTBAT",
                                     "fault_class": "batch_abort", "evidence": ["INC-0001", "INC-0002"], "status": "requested",
                                     "version_before": 1, "ts": now()})
    think("sentinel-1", "sentinel", "batch_abort resolved twice on mainframe. Asking the Gardener for a permanent fix.", None, "mainframe", "upgrade")
    agent("gardener-1", status="working", target="workshop", phase="planning")
    T(2)
    S().upgrade_requests.update_one({"_id": upg}, {"$set": {"status": "planning"}})
    T(4)
    S().upgrade_requests.update_one({"_id": upg}, {"$set": {"status": "testing", "diff": DIFF}})
    think("gardener-1", "gardener", "Diff ready (8 lines in systems/mainframe.py). Running tests + regression replay in an isolated DB.", None, "mainframe", "upgrade")
    T(6.5)
    S().approvals.insert_one({"_id": "APR-0003", "kind": "upgrade", "upgrade": upg, "requested_by": "gardener-1",
                              "desc": "Deploy batch record validation to mainframe", "risk": "code change, v1 → v2",
                              "status": "pending", "ts": now()})
    S().upgrade_requests.update_one({"_id": upg}, {"$set": {"status": "awaiting_approval", "approval_id": "APR-0003",
                                                            "tests": {"passed": True, "output": "12 passed, replay: 0 incidents"}}})
    wait_approval("APR-0003", T.scale(3))
    T.rebase()
    S().upgrade_requests.update_one({"_id": upg}, {"$set": {"status": "deploying"}})
    T(1.5)
    system("mainframe", version=2)
    S().playbooks.update_many({"system": "mainframe"}, {"$set": {"stale": True}})
    S().upgrade_requests.update_one({"_id": upg}, {"$set": {"status": "deployed", "version_after": 2}})
    think("sentinel-1", "sentinel", "mainframe is now v2: marked its playbooks stale until re-verified.", None, "mainframe", "learn")
    T(3.5)
    run = start_run("RUN-0003", "bank-night-3", 3, 1003)
    chaos("CHX-0005", "batch_abort", "mainframe", {}, run)
    log("mainframe", "WARN", "IEF196I", "1 record(s) quarantined pre-run (failed validation)")
    T(9)
    S().upgrade_requests.update_one({"_id": upg}, {"$set": {"status": "proven", "prove": {
        "fault": "batch_abort", "fired_at": now(), "window_s": 30, "incident_opened": False}}})
    think("gardener-1", "gardener", "Replayed batch_abort on v2: no incident. Upgrade proven.", None, "mainframe", "upgrade")
    agent("gardener-1", status="idle", phase=None)


class Clock:
    """Story clock: T(t) sleeps until t story-seconds after the current chapter started (scaled by --speed)."""

    def __init__(self, speed: float, until: float | None):
        self.speed, self.until, self.t0, self.elapsed = speed, until, time.monotonic(), 0.0

    def scale(self, s):
        return s / self.speed

    def rebase(self):
        self.elapsed += (time.monotonic() - self.t0) * self.speed
        self.t0 = time.monotonic()

    def __call__(self, t):
        target = self.t0 + t / self.speed
        if self.until is not None and self.elapsed + t > self.until:
            print(f"--until reached at story t={self.elapsed + t:.1f}s; holding state")
            raise SystemExit(0)
        while not STOP.is_set() and time.monotonic() < target:
            time.sleep(min(0.05, target - time.monotonic()))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--speed", type=float, default=1.0, help="story speed multiplier (2 = twice as fast)")
    ap.add_argument("--until", type=float, default=None, help="stop (and leave state) at this story second")
    ap.add_argument("--no-ensure", action="store_true", help="skip scripts.setup_db.ensure() (validators/indexes)")
    ap.add_argument("--force", action="store_true", help="allow writing to the real world/swarm DBs")
    args = ap.parse_args()
    if not args.force and (config.WORLD_DB in ("world",) or config.SWARM_DB in ("swarm",)):
        sys.exit("refusing to write the real 'world'/'swarm' DBs; set WORLD_DB/SWARM_DB or pass --force")
    print(f"fake_feed → {config.WORLD_DB} / {config.SWARM_DB} @ {config.MONGODB_URI.split('@')[-1]}")
    if not args.no_ensure:
        from scripts.setup_db import ensure
        ensure()
    baseline()
    for fn in (heartbeats, system_ticks):
        threading.Thread(target=fn, daemon=True).start()
    clock = Clock(args.speed, args.until)
    try:
        for chapter, pause in ((night1, 2.5), (night2, 2.5), (gardener, 1)):
            print(f"  chapter {chapter.__name__} (story t={clock.elapsed:.0f}s)")
            chapter(clock)
            clock(pause)
            clock.rebase()
        print(f"story complete at t={clock.elapsed:.0f}s")
    finally:
        STOP.set()


if __name__ == "__main__":
    main()
