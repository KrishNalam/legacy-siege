"""The estate as a whole: cascade, fix order, honesty rules, chaos, vulns, reset, restarts."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import systems.mainframe as mf
from systems.base import CASCADE_TICKS, now_ms
from systems.tests.helpers import chaos, op, status, tick

ALL = ("mainframe", "unix", "portal")


def strict_validate(record: str) -> bool:
    """Stand-in for the Gardener's patch (fixed width + numeric amount)."""
    return len(record) == mf.RECORD_LEN and not mf.is_malformed(record)


@pytest.fixture()
def legacy(monkeypatch):
    """Pin the legacy (accept-everything) validator, whatever is on disk."""
    monkeypatch.setattr(mf, "validate_record", lambda record: True)


def cascade(c, max_ticks=20):
    chaos(c, "mainframe", "batch_abort")
    for _ in range(max_ticks):
        tick(c)
        if status(c, "portal")[0] == "degraded":
            return
    raise AssertionError("portal never degraded")


def test_batch_abort_cascades_portal_unix_mainframe(c, legacy):
    chaos(c, "mainframe", "batch_abort")
    seen = {}
    for t in range(1, 15):
        tick(c)
        for s in ALL:
            if status(c, s)[0] != "up" and s not in seen:
                seen[s] = t
        assert status(c, "mainframe") == ("up", None)  # health never reveals the ABEND
    assert c.get("/mainframe/metrics").json()["batch_status"] == "ABENDED"
    assert any("IEF450I NIGHTBAT ABEND=S0C7" in ln["line"] for ln in c.get("/mainframe/logs").json()["lines"])
    assert status(c, "unix") == ("degraded", "mainframe")
    assert status(c, "portal") == ("degraded", "unix")
    assert "mainframe" not in seen and seen["unix"] < seen["portal"]
    assert 1 <= seen["portal"] - seen["unix"] <= CASCADE_TICKS  # portal tolerates a few stale ticks
    # the portal still answers 200 — with stale balances
    st = c.get("/portal/state").json()
    assert st["cache"]["stale"] is True
    r = c.get("/portal/view/balance")
    assert r.status_code == 200 and "<asOf>" in r.text


def test_wrong_order_fails_and_full_fix_order_restores_all_up(c, legacy):
    cascade(c)
    poison = c.get("/mainframe/state").json()["batch"]["poison_records"]
    assert len(poison) == 1
    # out of order: each of these must not claim success
    assert op(c, "mainframe", "restart_batch", {"job": "NIGHTBAT"})["applied"] is False  # poison still queued
    assert op(c, "unix", "rerun_transfer")["applied"] is False                          # no new batch file
    assert op(c, "portal", "refresh_cache")["applied"] is False                         # feed still stale

    assert op(c, "mainframe", "quarantine_record", {"record_id": poison[0]})["applied"]
    b = c.get("/mainframe/state").json()["batch"]
    assert poison[0] in b["quarantined"] and poison[0] not in b["poison_records"]

    assert op(c, "mainframe", "restart_batch", {"job": "NIGHTBAT"})["applied"]
    mst = c.get("/mainframe/state").json()["batch"]
    assert mst["status"] == "OK"

    assert op(c, "unix", "rerun_transfer")["applied"]
    ust = c.get("/unix/state").json()["transfer"]
    assert ust["last_file_batch_ts"] >= mst["last_run_ok"] and ust["status"] == "ok"

    assert op(c, "portal", "refresh_cache")["applied"]
    assert c.get("/portal/state").json()["cache"]["stale"] is False
    assert [status(c, s)[0] for s in ALL] == ["up", "up", "up"]
    tick(c, 6)  # and it stays healed
    assert [status(c, s)[0] for s in ALL] == ["up", "up", "up"]
    assert c.get("/mainframe/health").json()["status"] == "up"
    faults = c.app.state.estate.db.systems.find_one({"_id": "mainframe"})["faults_active"]
    assert faults == []


def test_lying_success_one_op_claims_applied_without_effect(c, legacy, wdb):
    cascade(c)
    chaos(c, "mainframe", "lying_success", {"count": 1})
    rid = c.get("/mainframe/state").json()["batch"]["poison_records"][0]
    r = op(c, "mainframe", "quarantine_record", {"record_id": rid}, action_id="INC-0001-s1")
    assert r == {"accepted": True, "applied": True, "message": r["message"], "action_id": "INC-0001-s1"}
    b = c.get("/mainframe/state").json()["batch"]
    assert rid in b["poison_records"] and rid not in b["quarantined"]  # ACK != APPLIED
    assert wdb.op_receipts.find_one({"_id": "INC-0001-s1"})["lied"] is True
    # retrying the same attempt returns the same (lying) receipt, still no effect
    assert op(c, "mainframe", "quarantine_record", {"record_id": rid}, action_id="INC-0001-s1")["applied"] is True
    assert rid in c.get("/mainframe/state").json()["batch"]["poison_records"]
    # a new attempt (new action_id) really applies — the lie budget is spent
    assert op(c, "mainframe", "quarantine_record", {"record_id": rid}, action_id="INC-0001-s1-a2")["applied"]
    assert rid in c.get("/mainframe/state").json()["batch"]["quarantined"]


def test_same_action_id_twice_is_a_noop(c, legacy, wdb):
    cascade(c)
    rid = c.get("/mainframe/state").json()["batch"]["poison_records"][0]
    op(c, "mainframe", "quarantine_record", {"record_id": rid})
    seq = lambda: wdb.system_state.find_one({"_id": "mainframe:batch"})["data"]["run_seq"]  # noqa: E731
    r1 = op(c, "mainframe", "restart_batch", {"job": "NIGHTBAT"}, action_id="dup-1")
    s1 = seq()
    r2 = op(c, "mainframe", "restart_batch", {"job": "NIGHTBAT"}, action_id="dup-1")
    assert seq() == s1  # NIGHTBAT was not submitted a second time
    assert r2["duplicate"] and r2["applied"] == r1["applied"] is True
    assert wdb.op_receipts.count_documents({"_id": "dup-1"}) == 1


def test_slow_apply_lands_later_and_survives_restart(app, c, legacy, wdb):
    chaos(c, "portal", "slow_apply", {"delay_s": 30})
    chaos(c, "portal", "sqli_exploit")
    r = op(c, "portal", "apply_patch", {"patch": "parameterize_search"}, action_id="slow-patch")
    assert r["accepted"] and r["applied"] is False
    tick(c, 2)
    assert c.get("/portal/state").json()["search_patch"] == "none"  # accepted is not done
    # time passes (persisted apply_at), and the systems process is replaced by a fresh one
    wdb.op_receipts.update_one({"_id": "slow-patch"}, {"$set": {"apply_at": now_ms() - timedelta(seconds=1)}})
    from systems.app import create_app
    with TestClient(create_app(tick_s=0)) as fresh:
        tick(fresh)
        assert fresh.get("/portal/state").json()["search_patch"] == "parameterize_search"
        assert fresh.post("/portal/security/probe/portal-sqli-search").json()["exploitable"] is False
    rec = wdb.op_receipts.find_one({"_id": "slow-patch"})
    assert rec["applied_at"] is not None and rec["effective"] is True
    assert op(c, "portal", "apply_patch", {"patch": "parameterize_search"}, action_id="slow-patch")["applied"]


def test_gardener_patch_shrugs_off_batch_abort(c, monkeypatch):
    monkeypatch.setattr(mf, "validate_record", strict_validate)
    assert c.get("/mainframe/state").json()["validation_enabled"] is True
    chaos(c, "mainframe", "batch_abort")
    tick(c, 15)
    b = c.get("/mainframe/state").json()["batch"]
    assert b["status"] == "OK" and len(b["rejected"]) == 1 and b["poison_records"] == []
    assert [status(c, s)[0] for s in ALL] == ["up", "up", "up"]
    assert not any("ABEND" in ln["line"] for ln in c.get("/mainframe/logs").json()["lines"])


def test_unix_and_portal_own_faults(c):
    chaos(c, "unix", "disk_fill", {"pct": 97})
    tick(c)
    assert status(c, "unix") == ("degraded", None)
    assert any("No space left on device" in ln["line"] for ln in c.get("/unix/logs").json()["lines"])
    assert op(c, "unix", "purge_spool", {"path": "/var/spool/xfer"})["applied"]
    assert c.get("/unix/state").json()["spool"]["disk_pct"] < 80
    tick(c, 2)
    assert status(c, "unix") == ("up", None)

    chaos(c, "unix", "cron_stop")
    n_logs = len(c.get("/unix/logs?limit=500").json()["lines"])
    tick(c, 8)
    assert status(c, "unix") == ("degraded", None)
    assert len(c.get("/unix/logs?limit=500").json()["lines"]) == n_logs  # silent failure: no new lines
    assert op(c, "unix", "restart_cron", {"job": "xfer_pull"})["applied"]
    assert c.get("/unix/state").json()["cron"]["xfer_pull"] == "running"
    tick(c, 2)
    assert status(c, "unix")[0] == "up"
    assert status(c, "portal") == ("degraded", "unix")  # it went stale meanwhile
    assert op(c, "portal", "refresh_cache")["applied"] and status(c, "portal")[0] == "up"

    chaos(c, "portal", "session_leak")
    tick(c, 15)
    assert status(c, "portal") == ("degraded", None)
    assert op(c, "portal", "flush_sessions")["applied"]
    assert c.get("/portal/state").json()["sessions"] < 400 and status(c, "portal")[0] == "up"


def test_vulns_probes_and_exploits(c, wdb):
    probe = lambda s, v: c.post(f"/{s}/security/probe/{v}").json()["exploitable"]  # noqa: E731
    assert probe("mainframe", "mf-default-operator") and probe("unix", "unix-world-writable-conf")
    assert probe("portal", "portal-sqli-search") and probe("portal", "sqli_search")  # id or alias
    assert chaos(c, "unix", "config_exploit")["ok"]
    assert chaos(c, "portal", "sqli_exploit")["ok"]
    vulns = {v["id"]: v for s in ALL for v in wdb.systems.find_one({"_id": s})["vulns"]}
    assert vulns["unix-world-writable-conf"]["exploited"] and vulns["portal-sqli-search"]["exploited"]
    assert "203.0.113.66" in c.get("/unix/view/xfer.conf").text

    assert op(c, "unix", "chmod_config", {"file": "/etc/xfer.conf", "mode": "0640"})["applied"]
    assert c.get("/unix/state").json()["config"]["/etc/xfer.conf"] == "0640"
    assert op(c, "portal", "apply_patch", {"patch": "parameterize_search"})["applied"]
    assert op(c, "mainframe", "rotate_credential", {"account": "OPERATOR"})["applied"]
    assert not any(probe(s, v) for s, v in [("mainframe", "mf-default-operator"),
                                            ("unix", "unix-world-writable-conf"), ("portal", "portal-sqli-search")])
    assert chaos(c, "portal", "sqli_exploit")["ok"] is False  # patched: exploit retest fails
    assert all(not v["open"] for s in ALL for v in wdb.systems.find_one({"_id": s})["vulns"])
    assert c.post("/mainframe/_chaos/disk_fill", json={"params": {}}).status_code == 400


def test_chaos_clear_disarms_injectors(c, wdb):
    chaos(c, "mainframe", "lying_success", {"count": 3})
    chaos(c, "portal", "slow_apply", {"delay_s": 60})
    op(c, "portal", "flush_sessions")
    assert c.post("/_chaos/clear").json()["ok"]
    assert op(c, "mainframe", "rotate_credential")["applied"]
    assert c.get("/mainframe/state").json()["credentials"]["OPERATOR"] == "rotated"
    assert wdb.op_receipts.count_documents({"applied_at": None, "apply_at": {"$ne": None}}) == 0


def test_reset_restores_full_baseline(c, legacy, wdb):
    cascade(c)
    chaos(c, "unix", "config_exploit")
    chaos(c, "mainframe", "slow_apply")
    op(c, "mainframe", "rotate_credential")
    c.post("/_admin/reload/unix")
    assert c.post("/_admin/reset").status_code == 200
    for s in ALL:
        d = wdb.systems.find_one({"_id": s})
        assert (d["status"], d["version"], d["faults_active"]) == ("up", 1, [])
        assert all(v["open"] and not v["exploited"] for v in d["vulns"])
    assert wdb.op_receipts.count_documents({}) == 0
    assert c.get("/mainframe/state").json()["batch"]["status"] == "OK"
    assert c.get("/unix/state").json()["config"]["/etc/xfer.conf"] == "0666"
    tick(c, 10)
    assert [status(c, s)[0] for s in ALL] == ["up", "up", "up"]


def test_reload_swaps_live_code(c, tmp_path):
    before = c.get("/portal/health").json()["version"]
    r = c.post("/_admin/reload/portal").json()
    assert r == {"system": "portal", "version_before": before, "version_after": before + 1}
    assert c.post("/_admin/reload/vax").status_code == 404
    assert "class Portal" in c.get("/portal/source?raw=1").text
