"""Monitor (SRE / observability). Also runs the reaper (roles/reaper.py) in the same loop.

Every ~1 s it sweeps /health + /metrics of every system (HTTP only: it never reads faults_active or
chaos_events) and:
  * detect:     opens an incident for a contiguous unhealthy segment of the chain, or COALESCES into the
                active incident it touches ($addToSet affected). A unique partial index on
                correlation_key {active:true} makes "one incident per cascade" a database guarantee.
  * reaper:     see roles/reaper.py (stale heartbeat -> dead, leases released).
  * staleness:  a system's version went up -> its playbooks are marked stale:true.
  * recurrence: aggregation over resolved incidents; same fault_class on the same root system resolved in
                >= UPGRADE_AFTER distinct nights and no upgrade request yet -> files swarm.upgrade_requests.

The rare events (detect / upgrade, and the reaper's reap) each run as a small LangGraph graph on an event-scoped thread.
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pymongo.errors import DuplicateKeyError

from common.db import col, next_id, now
from common.events import say
from common import comms
from upgrader import help as upgrader_help

from .. import leases, policy, recovery, schema, settings
from ..harness import Agent, checkpointer, log
from .. import lessons
from . import reaper


class DetectState(TypedDict, total=False):
    kind: str                # "chain" (health cascade) | "security" (exploit seen in logs)
    segment: list[str]
    symptoms: dict           # system -> canonical symptom line
    incident: str | None
    action: str


class UpgradeState(TypedDict, total=False):
    system: str
    fault_class: str
    evidence: list[str]
    upgrade: str | None


def _current_run() -> dict:
    return col("stats").find_one({"_id": "current"}) or {}


def build_graphs(agent: Agent):
    sysc = agent.systems
    cp = checkpointer()

    def _unhealthy(sys: str) -> bool:
        try:
            return sysc.health(sys).get("status") != "up"
        except Exception:
            return False

    # ------------------------------------------------------------------ detect / coalesce
    def correlate(state: DetectState, config) -> dict:
        seg = state["segment"]
        if state.get("kind") == "security":
            inc = col("incidents").find_one({"active": True, "correlation_key": f"security:{seg[0]}"})
            return {"incident": inc["_id"] if inc else None}
        touch = set(seg).union(*[policy.neighbours(s) for s in seg])
        inc = col("incidents").find_one({"active": True, "kind": {"$ne": "security"}, "affected": {"$in": sorted(touch)}})
        return {"incident": inc["_id"] if inc else None}

    def open_or_grow(state: DetectState, config) -> dict:
        seg, sym = state["segment"], state["symptoms"]
        security = state.get("kind") == "security"
        for _ in range(3):
            if state.get("incident") and security:
                return {"action": "none"}  # already being handled
            if state.get("incident"):
                inc = col("incidents").find_one({"_id": state["incident"]})
                new = [s for s in seg if s not in (inc.get("affected") or [])]
                if not new or not inc.get("active"):
                    return {"action": "none"}
                affected = sorted(set(inc["affected"]) | set(seg), key=policy.rank)
                head = policy.most_downstream(affected)
                lines = sorted(set(inc.get("symptoms") or []) | {sym[s] for s in new},
                               key=lambda ln: policy.rank(ln.split(" ", 1)[0]))
                upd = {"$addToSet": {"affected": {"$each": new}}, "$set": {"symptoms": lines, "system": head}}
                try:  # the key follows the most-downstream unhealthy system
                    col("incidents").update_one({"_id": inc["_id"]}, {**upd, "$set": {**upd["$set"],
                                                "correlation_key": f"chain:{head}"}})
                except DuplicateKeyError:
                    col("incidents").update_one({"_id": inc["_id"]}, upd)
                agent.think(f"{', '.join(new)} also unhealthy and adjacent to {inc['_id']} — folding in "
                            f"(affected: {', '.join(affected)}).", incident=inc["_id"], system=new[0], pause=False)
                return {"action": "grew"}
            if not security:
                # re-check right before opening: a fix may have landed since this sweep's poll
                seg = [x for x in seg if _unhealthy(x)]
                if not seg:
                    return {"action": "none"}
            head = seg[0]
            run = _current_run()
            doc = {"_id": next_id("INC"), "active": True, "kind": "security" if security else "chain",
                   "correlation_key": f"{'security' if security else 'chain'}:{head}", "status": "open",
                   "system": head, "affected": list(seg), "symptoms": [sym[s] for s in seg],
                   "root_cause_system": None, "fault_class": None, "root_cause": None,
                   "owner": None, "lease_until": None, "generation": 0, "assigned_to": None,
                   "run_id": run.get("run_id"), "round": run.get("round"),
                   "recall": None, "diagnosis_path": [], "handoffs": [], "steps": [],
                   "opened_at": now(), "closed_at": None, "metrics": {}}
            try:
                col("incidents").insert_one(doc)
            except DuplicateKeyError:  # unique partial index: someone already holds this key -> coalesce
                state = {**state, **correlate(state, config)}
                continue
            agent.think(f"Opened {doc['_id']}: {doc['symptoms'][0]}.", kind="action", incident=doc["_id"],
                        system=head, pause=False)
            if comms.enabled():  # structure v3 step 1: the Monitor tells the Coordinator (the Coordinator still reads the incident)
                say(agent.id, "coordinator", "incident", {"incident": doc["_id"], "system": head, "affected": list(seg),
                                                    "text": f"Opened {doc['_id']} on {head}: {doc['symptoms'][0]}"},
                    incident=doc["_id"], system=head, severity="warning")
            if recovery.enabled() and not security:  # visible final recovery after an escalation
                prev = col("incidents").find_one({"status": "failed", "affected": {"$in": list(seg)}},
                                                 {"failure": 1}, sort=[("closed_at", -1)])
                if prev and not col("incidents").find_one({"_id": {"$gt": prev["_id"], "$ne": doc["_id"]},
                                                           "affected": {"$in": list(seg)}}, {"_id": 1}):
                    agent.think(f"Not over: {prev['_id']} was escalated ({(prev.get('failure') or 'failed')[:90]}) and "
                                f"{', '.join(seg)} still unhealthy, so {doc['_id']} starts over with a fresh "
                                f"diagnosis and plan.", kind="action", incident=doc["_id"], system=head, pause=False)
            return {"incident": doc["_id"], "action": "opened"}
        return {"action": "none"}

    g = StateGraph(DetectState)
    g.add_node("correlate", correlate)
    g.add_node("open_or_grow", open_or_grow)
    g.add_edge(START, "correlate")
    g.add_edge("correlate", "open_or_grow")
    g.add_edge("open_or_grow", END)
    detect = g.compile(checkpointer=cp)

    reap = reaper.build(agent, cp)

    # ------------------------------------------------------------------ recurrence -> upgrade request
    def file_request(state: UpgradeState, config) -> dict:
        sys, fc = state["system"], state["fault_class"]
        try:
            ver = sysc.version(sys)
        except Exception:
            ver = None
        uid = next_id("UPG")
        weakness = policy.WEAKNESS.get(fc, f"{sys} keeps failing with {fc}")
        r = col("upgrade_requests").update_one({"system": sys, "fault_class": fc}, {"$setOnInsert": {
            "_id": uid, "system": sys, "weakness": weakness, "fault_class": fc, "evidence": state["evidence"],
            "status": "requested", "diff": None, "tests": None, "approval_id": None, "version_before": ver,
            "version_after": None, "prove": None, "requested_by": agent.id, "ts": now()}}, upsert=True)
        if r.upserted_id is None:
            return {"upgrade": None}
        agent.think(f"{fc} on {sys} resolved {len(state['evidence'])} times ({', '.join(state['evidence'])}) — "
                    f"filed {uid} for the Upgrader: {weakness}.", kind="upgrade", system=sys, pause=False)
        say(agent.id, "upgrader", "upgrade", {"upgrade": uid, "system": sys, "weakness": weakness})
        return {"upgrade": uid}

    g = StateGraph(UpgradeState)
    g.add_node("file_request", file_request)
    g.add_edge(START, "file_request")
    g.add_edge("file_request", END)
    upgrade = g.compile(checkpointer=cp)
    return detect, reap, upgrade


SAMPLE_EVERY_S = 10.0   # known-good feed sample (4 MongoDB round trips)
FEED_CHECK_S = 2.0      # feed field check (1 state read + 1 find): notices a drift before the portal breaks

RECURRING = [
    {"$match": {"status": "resolved", "fault_class": {"$nin": [None, "unknown", "schema_drift"]}}},
    {"$sort": {"closed_at": 1}},
    # recurrence = across nights: a fault re-struck within one night (e.g. by the Red fleet) is one attack
    # wave, not a recurring weakness yet (incidents without a run count one by one, as before)
    {"$group": {"_id": {"fault_class": "$fault_class", "system": "$root_cause_system"},
                "nights": {"$addToSet": {"$ifNull": ["$run_id", "$_id"]}}, "evidence": {"$push": "$_id"}}},
    {"$match": {"$expr": {"$gte": [{"$size": "$nights"}, settings.UPGRADE_AFTER]}}},
    {"$lookup": {"from": "upgrade_requests", "let": {"fc": "$_id.fault_class", "sys": "$_id.system"},
                 "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$fault_class", "$$fc"]},
                                                             {"$eq": ["$system", "$$sys"]}]}}}],
                 "as": "existing"}},
    {"$match": {"existing": {"$size": 0}}},
]


MONITOR_2 = "monitor-2"


def monitor_2_alive() -> bool:
    """monitor-1 hands its Monitor #2 duties over while a live monitor-2 heartbeats (and takes them back if it dies)."""
    return bool(col("agents").find_one({"_id": MONITOR_2, "status": {"$ne": "dead"},
                                        "last_seen": {"$gte": now() - timedelta(seconds=settings.DEAD_AFTER_S)}},
                                       {"_id": 1}))


def run_monitor_2(agent: Agent) -> None:
    """Structure v3 Monitor #2 (UPGRADER_HELP=1): sits between Comms and the Upgrader and cuts duplicate work —
    pushes remembered fixes to the Investigators and hands repeats to the Upgrader. Detection stays on monitor-1."""
    agent.set(target="hive", phase="dedup_watch")
    last_slow = 0.0
    while not agent.stop.is_set():
        t0 = time.monotonic()
        try:
            versions = {}
            for s in policy.systems():
                try:
                    versions[s] = agent.systems.version(s)
                except Exception:
                    pass
            push_known_fixes(agent, versions)
            if time.monotonic() - last_slow > 3:
                last_slow = time.monotonic()
                note_repeated_problems(agent)
        except SystemExit:
            raise
        except Exception as e:
            log(agent.id, f"monitor-2 sweep error: {type(e).__name__}: {e}")
        agent.nap(max(0.05, settings.MONITOR_POLL_S - (time.monotonic() - t0)))


def run(agent: Agent) -> None:
    if agent.id == MONITOR_2:
        return run_monitor_2(agent)
    if agent.id.startswith("toolsmith-"):  # toolsmith engineers are Monitor-type agents (TOOLSMITH_ENGINEERS)
        from . import toolsmith as toolsmith_role
        return toolsmith_role.run(agent)
    detect, reap, upgrade = build_graphs(agent)
    sysc = agent.systems
    streak: dict[str, int] = {}
    versions: dict[str, int] = {}
    last_slow = 0.0
    last_sample = 0.0
    last_feed = 0.0

    last_scan: dict = {"at": 0.0, "since": {}, "seen": set()}

    def security_scan() -> None:
        """Health never shows an exploit. Scan new log lines for signs of a vuln being USED, confirm with the
        security probe, and open a 'security' incident for that system."""
        for s in policy.systems():
            since = last_scan["since"].get(s) or (now() - timedelta(seconds=60)).isoformat()
            try:
                logs = sysc.logs(s, limit=50, since=since)
            except Exception:
                continue
            if logs:
                last_scan["since"][s] = max(str(ln.get("ts")) for ln in logs)
            signs = [ln for ln in logs if policy.exploit_signs(s, [ln])
                     and (s, ln.get("ts"), ln.get("line")) not in last_scan["seen"]]
            if not signs:
                continue
            last_scan["seen"].update((s, ln.get("ts"), ln.get("line")) for ln in signs)
            vid = policy.VULNS.get(s)
            try:
                exploitable = bool(sysc.probe(s, vid).get("exploitable")) if vid else False
            except Exception:
                exploitable = False
            if not exploitable:
                continue
            line = f"{s} exploit signature in logs, {vid} probe exploitable"
            agent.think(f"Security: {s} log shows '{signs[-1].get('line', '')[:70]}…' and {vid} is exploitable.",
                        kind="thought", system=s, pause=False)
            detect.invoke({"kind": "security", "segment": [s], "symptoms": {s: line}},
                          {"configurable": {"thread_id": f"monitor:security:{s}:{int(time.time() * 1000)}"}},
                          durability="exit")

    noticed: dict = {"change": None}

    def notice_feed_change(st: dict) -> None:
        """Say it the moment a feed sample stops matching the expected schema (before the portal goes stale)."""
        ch = schema.feed_change(st)
        if ch is None or ch == noticed["change"]:
            noticed["change"] = ch
            return
        noticed["change"] = ch
        missing, added = ch
        what = ", ".join([f"{f} missing" for f in missing] + [f"{f} new" for f in added])
        agent.think(f"{schema.SYSTEM} feed schema changed: {what} ({schema.FEED} no longer matches the expected "
                    f"schema; {schema.CONSUMER} reads it next).", kind="thought", system=schema.SYSTEM, pause=False)

    def sweep() -> None:
        nonlocal last_slow, last_sample, last_feed
        health, lines = {}, {}
        for s in policy.systems():
            try:
                h = sysc.health(s)
                m = sysc.metrics(s)
            except Exception:
                continue  # unreachable: don't guess
            health[s] = h
            lines[s] = policy.symptom_line(s, h, m)
            streak[s] = streak.get(s, 0) + 1 if h.get("status") != "up" else 0
            v = int(h.get("version") or 1)
            if versions.get(s) != v:
                # version bump (e.g. Upgrader upgrade) -> playbooks learned on older versions go stale
                r = col("playbooks").update_many({"system": s, "stale": False, "system_version": {"$lt": v}},
                                                 {"$set": {"stale": True, "staled_at": now(),
                                                           "stale_reason": f"{s} upgraded to v{v}"}})
                if r.modified_count:
                    agent.think(f"{s} is now v{v} (was v{versions.get(s, v - 1)}): marked {r.modified_count} "
                                f"playbook(s) stale until re-verified.", kind="learn", system=s, pause=False)
                if lessons.enabled():  # failure memory carries versions too: old lessons retire with the code
                    n = lessons.mark_stale(s, v)
                    if n:
                        agent.think(f"{s} is now v{v}: marked {n} lesson(s) learned on older versions stale — "
                                    f"what failed on the old code may not fail on the new.", kind="learn",
                                    system=s, pause=False)
                versions[s] = v
        confirmed = [s for s in health if streak.get(s, 0) >= settings.CONFIRM_POLLS]
        for seg in policy.segments(confirmed):
            if col("incidents").find_one({"active": True, "kind": {"$ne": "security"}, "affected": {"$all": seg}},
                                         {"_id": 1}):
                continue  # already covered by an active incident
            if col("incidents").find_one({"status": "failed", "kind": {"$ne": "security"}, "affected": {"$in": seg},
                                          "closed_at": {"$gt": now() - timedelta(seconds=settings.FAILED_COOLDOWN_S)}},
                                         {"_id": 1}):
                continue  # just escalated to the approver (e.g. approval denied): don't nag immediately
            key = f"{seg[0]}:{int(time.time() * 1000)}"
            detect.invoke({"kind": "chain", "segment": seg, "symptoms": {s: lines[s] for s in seg}},
                          {"configurable": {"thread_id": f"monitor:detect:{key}"}}, durability="exit")

        reaper.sweep(agent, reap)  # stale heartbeats -> dead, leases released

        if time.monotonic() - last_scan["at"] > settings.SECURITY_SCAN_S:
            last_scan["at"] = time.monotonic()
            security_scan()

        both_up = all((health.get(s) or {}).get("status") == "up" for s in (schema.SYSTEM, schema.CONSUMER))
        if both_up and time.monotonic() - last_feed > FEED_CHECK_S:
            last_feed = time.monotonic()
            try:
                st = sysc.state(schema.SYSTEM)
                notice_feed_change(st)
                if time.monotonic() - last_sample > SAMPLE_EVERY_S:
                    # known-good feed sample for schema-drift diffs (only kept if it matches the expected schema)
                    last_sample = time.monotonic()
                    schema.capture_good(st)
            except Exception as e:
                log(agent.id, f"schema sample error: {type(e).__name__}: {e}")

        delegated = upgrader_help.enabled() and monitor_2_alive()  # Monitor #2 duties live on monitor-2
        if upgrader_help.enabled() and not delegated:  # fallback: push a remembered fix to the Investigator
            try:
                push_known_fixes(agent, versions)
            except Exception as e:
                log(agent.id, f"known-fix push error: {type(e).__name__}: {e}")

        if time.monotonic() - last_slow > 3:
            last_slow = time.monotonic()
            if not delegated:
                note_repeated_problems(agent)
            for row in col("incidents").aggregate(RECURRING):
                upgrade.invoke({"system": row["_id"]["system"], "fault_class": row["_id"]["fault_class"],
                                "evidence": row["evidence"]},
                               {"configurable": {"thread_id": f"monitor:upgrade:{row['_id']['fault_class']}:"
                                                              f"{row['_id']['system']}"}}, durability="exit")

    agent.set(target="hive")
    while not agent.stop.is_set():
        agent.pause_point()
        t0 = time.monotonic()
        try:
            sweep()
        except SystemExit:
            raise
        except Exception as e:
            log(agent.id, f"sweep error: {type(e).__name__}: {e}")
        agent.nap(max(0.05, settings.MONITOR_POLL_S - (time.monotonic() - t0)))


def note_repeated_problems(agent):
    """Monitor #2 mode: durable advisory notes, independent of FAILURE_MEMORY. With UPGRADER_HELP=1 each
    problem seen 3+ times is also handed to the Upgrader as a real request (upgrader.help answers it)."""
    handoff = upgrader_help.enabled()
    if not (comms.enabled() or handoff):
        return
    groups, runs = {}, {}
    # Only resolved incidents, one count per night (run_id): failed incidents and Red re-strikes within one night
    # must not push a problem to 3 and start an upgrade before UPGRADE_AFTER / distinct nights would (RV1b).
    for inc in col('incidents').find({'status': 'resolved'},
                                     {'root_cause_system': 1, 'system': 1, 'fault_class': 1, 'symptoms': 1, 'run_id': 1}):
        sig = lessons.problem_signature(inc)
        g = groups.setdefault('incident:' + sig, {'system': inc.get('root_cause_system') or inc.get('system'),
            'problem': inc.get('fault_class') or '; '.join(inc.get('symptoms') or [])[:160], 'evidence': [], 'count': 0,
            'fault_class': inc.get('fault_class') if inc.get('fault_class') not in (None, 'unknown') else None})
        night = inc.get('run_id') or inc['_id']
        seen = runs.setdefault('incident:' + sig, set())
        if night in seen:
            continue
        seen.add(night)
        g['count'] += 1
        g['evidence'].append(inc['_id'])
    for ls in col('lessons').find({'count': {'$gte': 3}, 'stale': False}):
        groups['lesson:' + ls['_id']] = {'system': ls.get('system'), 'problem': ls.get('why'),
            'count': ls['count'], 'evidence': ls.get('incidents') or []}
    for sig, g in groups.items():
        if g['count'] < 3:
            continue
        text = f"sent to the Upgrader: {g['problem']}, seen {g['count']} times"
        result = col('gardener_notes').update_one({'_id': sig},
            {'$setOnInsert': {'source': agent.id, 'ts': now()},
             '$set': {**g, 'text': text, 'updated_at': now()}}, upsert=True)
        if result.upserted_id is not None:
            say(agent.id, 'upgrader', 'upgrader_note', text, system=g['system'])
            agent.think(text, kind='upgrader_note', system=g['system'], pause=False)
        if handoff and upgrader_help.hand_off_repeat(agent.id, sig, g) is not None:
            where = f" on {g['system']}" if g.get('system') and g['system'] not in str(g['problem']) else ''
            agent.think(f"Monitor #2 → Upgrader: {g['problem']}{where} repeated {g['count']}× "
                        f"({', '.join(g['evidence'][-4:])}).", kind='upgrade', system=g['system'], pause=False)


def known_fix_for(symptoms: list[str], versions: dict) -> dict | None:
    """The newest current playbook whose remembered symptom lines match these verbatim (symptom lines are
    canonical and number-free, so the same fault reads the same way twice). One shared downstream line that
    doesn't point at the playbook's root system is not enough."""
    lines = set(symptoms or [])
    if not lines:
        return None
    for pb in col("playbooks").find({"stale": False, "system": {"$ne": None}},
                                    {"embedding": 0}).sort("verified_at", -1).limit(50):
        seen, root = set(pb.get("symptoms") or []), pb["system"]
        cur = versions.get(root)
        # whole remembered cascade present, or an early part of it that already points at the remembered root
        # (the Investigator's confirm hop checks the fault class either way, so a near-miss costs one read)
        early = lines <= seen and any(ln.split(" ", 1)[0] == root or f"blames {root}" in ln for ln in lines)
        if seen and (seen <= lines or early) and (cur is None or pb.get("system_version") == cur) and pb.get("fix"):
            return pb
    return None


def push_known_fixes(agent, versions: dict) -> None:
    """Monitor #2 cuts duplicate work: when an Investigator has taken an incident whose symptoms match a fix the
    hive already verified, push that fix onto the incident (known_fix) and the bus, so the Investigator uses it instead
    of rediscovering it. The Investigator still confirms it with one read and the Verifier still verifies every step."""
    for inc in col("incidents").find({"active": True, "kind": {"$ne": "security"}, "owner": {"$ne": None},
                                      "status": {"$in": ["claimed", "diagnosing"]}, "known_fix": {"$exists": False}},
                                     {"symptoms": 1, "owner": 1}):
        pb = known_fix_for(inc.get("symptoms"), versions)
        if pb is None:
            continue
        fix = {"from": agent.id, "source": "playbook", "playbook_id": pb["_id"], "system": pb["system"],
               "fault_class": pb.get("fault_class"), "system_version": pb.get("system_version"),
               "learned_in": pb.get("incident"), "fix": pb.get("fix") or [], "to": inc["owner"], "ts": now()}
        r = col("incidents").update_one({"_id": inc["_id"], "known_fix": {"$exists": False}}, {"$set": {"known_fix": fix}})
        if not r.modified_count:
            continue
        say(agent.id, inc["owner"], "known_fix", {k: fix[k] for k in ("playbook_id", "system", "fault_class")},
            incident=inc["_id"], system=pb["system"])
        agent.think(f"Monitor #2 → {inc['owner']}: {inc['_id']} shows the same symptoms as {pb.get('incident')} "
                    f"({pb.get('fault_class')} on {pb['system']}); pushed known fix {pb['_id']} so nobody rediscovers it.",
                    kind="recall", incident=inc["_id"], system=pb["system"], pause=False)
