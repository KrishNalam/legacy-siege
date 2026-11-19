"""schema_drift: unix silently renames a manifest field; the portal breaks until its mapping is re-pointed."""
from systems.base import CASCADE_TICKS
from systems.tests.helpers import chaos, op, status, tick


def test_schema_drift_breaks_portal_until_mapped(c, wdb):
    tick(c, 3)
    s0 = c.get("/unix/state").json()["feed"]
    assert s0["schema_version"] == 1 and s0["sample"] and "balance" in s0["sample"][0]

    r = chaos(c, "unix", "schema_drift")
    assert r["ok"] and "balance -> acct_bal" in r["effect"]
    feed = c.get("/unix/state").json()["feed"]
    assert feed["schema_version"] == 2
    assert all("acct_bal" in rec and "balance" not in rec for rec in feed["sample"])

    for _ in range(CASCADE_TICKS * 4):
        tick(c)
        if status(c, "portal")[0] == "degraded":
            break
    assert status(c, "portal") == ("degraded", "unix")
    assert status(c, "unix") == ("up", None)                      # the source never notices
    h = c.get("/portal/health").json()
    assert c.get("/portal/metrics").json()["error_rate"] >= 0.05
    pst = c.get("/portal/state").json()
    assert pst["cache"]["stale"] and pst["feed"]["errors"] >= CASCADE_TICKS and h["status"] == "degraded"
    assert any(ln["code"] == "SOAP-E-PARSE" for ln in c.get("/portal/logs").json()["lines"])

    # refreshing without a new mapping cannot work
    assert op(c, "portal", "refresh_cache")["applied"] is False
    assert op(c, "portal", "map_field", {"field": "balance", "source": "nope!"})["applied"] is False
    assert op(c, "portal", "map_field", {"field": "balance", "source": "acct_bal"})["applied"] is True
    assert c.get("/portal/state").json()["feed"]["map"] == {"balance": "acct_bal"}
    assert op(c, "portal", "refresh_cache")["applied"] is True
    tick(c, 3)
    assert status(c, "portal") == ("up", None)
    assert c.get("/portal/state").json()["feed"]["errors"] == 0
    assert not [f for f in wdb.systems.find_one({"_id": "unix"})["faults_active"] if f["fault"] == "schema_drift"]

    # a second patch renames again (v3), reset restores v1
    assert "acct_bal -> ledger_bal" in chaos(c, "unix", "schema_drift")["effect"]
    assert c.post("/_admin/reset").status_code == 200
    assert c.get("/unix/state").json()["feed"]["schema_version"] == 1
    assert c.get("/portal/state").json()["feed"]["map"] == {"balance": "balance"}


def test_manifest_view_is_json(c):
    tick(c, 3)
    r = c.get("/unix/view/manifest")
    assert r.status_code == 200 and r.json()["records"][0].keys() >= {"acct", "balance", "as_of"}
