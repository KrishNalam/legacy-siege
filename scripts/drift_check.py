"""Schema-drift rehearsal: drives the war-room API and checks the adaptation beat in MongoDB.

    .venv/bin/python -m scripts.drift_check                     # full reset, bank-drift, 2nd drift, full reset
    .venv/bin/python -m scripts.drift_check --once --keep       # one drift, leave it on screen
    .venv/bin/python -m scripts.drift_check --base http://127.0.0.1:8740

Also reused by scripts.demo_check --drift; the default demo stays unchanged. Beats checked:
  a. bank-drift renames unix's manifest field balance -> acct_bal; unix stays UP, the portal degrades blaming unix
  b. one incident; the walk portal -> unix finds schema_drift on unix (MongoDB $jsonSchema + field diff)
  c. map_field + refresh_cache verified by verifier-1 with read-back evidence (values line up)
  d. swarm.schema_mappings holds balance -> acct_bal at v2; the verifier says "mapping learned v2"
  e. incident resolved, portal up, playbook saved; no Upgrader upgrade request for schema_drift
  f. (default) a second vendor patch acct_bal -> ledger_bal is diffed against the LEARNED schema -> v3
  g. (default) next round, the same balance -> acct_bal patch again: the round reset kept the verified mapping
     history, so the unix hop says "seen before: mapping v2 reused" (no Claude call), the Verifier reuses v2, and
     $vectorSearch recall makes it 1 hop
With AI_SECOND_OPINION=1 + a key on the stack, an extra row checks incident.ai (kind schema_mapping, Verifier verdict).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import config  # noqa: E402
from scripts.demo_check import Demo, Report, log  # noqa: E402

MID = "unix.xfer_manifest.balance"


class Drift(Demo):
    def wait_good_sample(self) -> bool:
        got, secs = self.wait("good sample", lambda: self.s.schema_samples.find_one({"_id": "good:unix:xfer_manifest"}),
                              30)
        return self.r.add("good sample", bool(got), "monitor stored a known-good xfer_manifest sample"
                          if got else "no good sample captured in 30s", secs)

    def drift(self, start, old: str, new: str, version: int, timeout: float) -> bool:
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        start()
        unix_down, portal_bad, inc_id = False, None, None
        while time.monotonic() - t0 < timeout:
            systems = {d["_id"]: d for d in self.w.systems.find()}
            ux, po = systems.get("unix", {}), systems.get("portal", {})
            unix_down |= ux.get("status") != "up"
            if portal_bad is None and po.get("status") == "degraded" and po.get("blames") == "unix":
                portal_bad = time.monotonic() - t0
                portal_bad_at = datetime.now(timezone.utc)
                log(f"portal degraded blaming unix at {portal_bad:.1f}s (unix {ux.get('status')})")
            inc = self.s.incidents.find_one({"fault_class": "schema_drift", "steps.params.source": new},
                                            sort=[("opened_at", -1)])
            if inc:
                inc_id = inc["_id"]
                if not inc.get("active"):
                    break
            time.sleep(0.5)
        secs = time.monotonic() - t0
        early = self.s.thoughts.find_one({"agent": {"$regex": "^monitor"}, "text": {"$regex": f"feed schema changed: "
                                          f"{old} missing"}, "ts": {"$gte": started}}, sort=[("ts", -1)])
        if early:
            log(f"monitor noticed early: {early['text'][:90]}")
        self.r.add(f"v{version} monitor before portal", bool(early) and portal_bad is not None
                   and early["ts"] < portal_bad_at,
                   f"monitor notice={(early or {}).get('ts')}; portal degradation observed="
                   f"{portal_bad_at if portal_bad is not None else 'never'}")
        feed = self.sys_http.get("/unix/state").json().get("feed") or {}
        rec = (feed.get("sample") or [{}])[0]
        self.r.add(f"v{version} a. drift", portal_bad is not None and not unix_down and new in rec and old not in rec,
                   f"manifest field {old} → {new} (schema v{feed.get('schema_version')}); unix stayed up={not unix_down}; "
                   f"portal degraded at {portal_bad if portal_bad is None else round(portal_bad, 1)}s; monitor noticed "
                   f"first: {bool(early)}", portal_bad)
        inc = self.s.incidents.find_one({"_id": inc_id}) if inc_id else None
        if not inc:
            return self.r.add(f"v{version} b. diagnosis", False, f"no schema_drift incident within {timeout:.0f}s", secs)
        recall = (inc.get("recall") or {}).get("used")
        self.r.add(f"v{version} b. diagnosis", inc.get("root_cause_system") == "unix"
                   and (inc.get("diagnosis_path") or [None])[-1] == "unix",
                   f"{inc_id}: path {'→'.join(inc.get('diagnosis_path') or [])}, recall used={recall}; "
                   f"{inc.get('root_cause')}")
        st = {s["op"]: s for s in inc.get("steps") or []}
        mf, rc = st.get("map_field") or {}, st.get("refresh_cache") or {}
        led = self.s.ledger.find_one({"action_id": mf.get("action_id")}) or {}
        self.r.add(f"v{version} c. verifier", mf.get("status") == "verified" and rc.get("status") == "verified"
                   and "line up" in (mf.get("evidence") or "") and led.get("status") == "verified",
                   f"map_field(source={mf.get('params', {}).get('source')}) {mf.get('status')}: "
                   f"{(mf.get('evidence') or '')[:90]}")
        m = self.s.schema_mappings.find_one({"_id": MID}) or {}
        said = self.s.thoughts.find_one({"kind": "learn", "agent": "verifier-1",
                                         "text": {"$regex": f"{old} → {new} · mapping learned v{version}"}})
        self.r.add(f"v{version} d. mapping", m.get("source") == new and m.get("version") == version and bool(said),
                   f"schema_mappings {MID}: source={m.get('source')} v{m.get('version')} "
                   f"history={len(m.get('history') or [])}; war room line: {(said or {}).get('text', 'MISSING')[:80]}")
        health = {s: self.sys_http.get(f"/{s}/health").json().get("status") for s in ("unix", "portal")}
        ai = (self.s.incidents.find_one({"_id": inc_id}, {"ai": 1}) or {}).get("ai")
        if ai:
            self.r.add(f"v{version} ai", ai.get("kind") == "schema_mapping"
                       and (ai.get("verifier") or {}).get("verdict") == "verified",
                       f"proposed_by={ai.get('proposed_by')} model={ai.get('model')} "
                       f"verdict={(ai.get('verdicts') or [{}])[0].get('verdict')} verifier={(ai.get('verifier') or {}).get('reason')}")
        upg = self.s.upgrade_requests.count_documents({"fault_class": "schema_drift"})
        return self.r.add(f"v{version} e. resolved", inc.get("status") == "resolved" and bool(inc.get("playbook_id"))
                          and all(v == "up" for v in health.values()) and upg == 0,
                          f"{inc.get('status')} TTR {(inc.get('metrics') or {}).get('ttr_s')}s, playbook "
                          f"{inc.get('playbook_id')}, health {health}, upgrade requests {upg}", secs)


def _repeat(self, timeout: float) -> bool:
    """Next round (estate back to v1, live mapping rewound to v1, history + playbooks kept), same rename again."""
    log("POST /api/reset {mode: next_round}")
    self.post("/api/reset", {"mode": "next_round"})
    if not self.wait_good_sample():
        return False
    t0, started = time.monotonic(), datetime.now(timezone.utc)
    self.post("/api/scenario/bank-drift")
    inc, secs = self.wait("repeat", lambda: self.s.incidents.find_one(
        {"fault_class": "schema_drift", "active": False, "opened_at": {"$gt": started}}), timeout)
    if not inc:
        return self.r.add("repeat g. memory", False, f"no resolved repeat incident in {timeout:.0f}s",
                          time.monotonic() - t0)
    said = self.s.tasks.find_one({"incident": inc["_id"], "kind": "diagnose",
                                  "findings.params.proposed_by": "memory"}, {"findings.facts": 1})
    said = said and {"text": next((f for f in said["findings"]["facts"] if f.startswith("seen before:")), "")}
    m = self.s.schema_mappings.find_one({"_id": MID}) or {}
    verifier = self.s.thoughts.find_one({"incident": inc["_id"], "kind": "learn",
                                      "text": {"$regex": "seen before: mapping v2 reused"}})
    return self.r.add("repeat g. memory", inc["status"] == "resolved" and (inc.get("recall") or {}).get("used")
                      and len(inc.get("diagnosis_path") or []) == 1 and bool(said and said["text"])
                      and m.get("source") == "acct_bal" and m.get("version") == 2 and m.get("reused") is True
                      and bool(verifier),
                      f"{inc['_id']}: recall used={(inc.get('recall') or {}).get('used')}, path "
                      f"{'→'.join(inc.get('diagnosis_path') or [])}, TTR {(inc.get('metrics') or {}).get('ttr_s')}s, "
                      f"mapping v{m.get('version')} {m.get('source')} reused={m.get('reused')} "
                      f"history={len(m.get('history') or [])}; {(said or {}).get('text') or 'no seen-before line'}"[:230],
                      time.monotonic() - t0)


Drift.repeat = _repeat


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("CONSOLE_URL", f"http://127.0.0.1:{config.CONSOLE_PORT}"))
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--keep", action="store_true", help="don't full-reset at the end")
    ap.add_argument("--once", action="store_true", help="skip the second drift (v3) and the repeat round")
    ap.add_argument("--timeout", type=float, default=150)
    a = ap.parse_args()
    rep = Report()
    d = Drift(a.base, 1.5, rep)
    log(f"drift_check against {a.base} (systems {config.SYSTEMS_URL}, db {config.WORLD_DB}/{config.SWARM_DB})")
    try:
        if d.preflight(reset=not a.no_reset) and d.wait_good_sample():
            ok = d.drift(lambda: d.post("/api/scenario/bank-drift"), "balance", "acct_bal", 2, a.timeout)
            if ok and not a.once:
                # no wait: the Verifier re-captured the good sample when it stored v2 (review fix 1)
                g = d.s.schema_samples.find_one({"_id": "good:unix:xfer_manifest"}) or {}
                ok = rep.add("v2 good sample", "acct_bal" in (g.get("record") or {}),
                             f"good sample right after v2 carries acct_bal: {'acct_bal' in (g.get('record') or {})}")
                ok = ok and d.drift(lambda: d.post("/api/chaos/schema_drift", {"target": "unix", "params": {}}),
                             "acct_bal", "ledger_bal", 3, a.timeout)
            if ok and not a.once:
                d.repeat(a.timeout)
        if not a.keep:
            log("POST /api/reset {mode: full}")
            d.post("/api/reset", {"mode": "full"})
    except KeyboardInterrupt:
        rep.add("aborted", False, "interrupted")
    except Exception as e:
        import traceback
        traceback.print_exc()
        rep.add("crashed", False, f"{type(e).__name__}: {e}")
    return 0 if rep.table() else 1


if __name__ == "__main__":
    sys.exit(main())
