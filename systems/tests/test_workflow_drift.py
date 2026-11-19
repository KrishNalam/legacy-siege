"""workflow_drift (mainframe): the catalog changes, restart_batch is refused until release_hold runs."""
from systems.tests.helpers import adversary, op, tick


def _abend(c):
    chaos(c, "mainframe", "batch_abort")
    for _ in range(6):
        tick(c)
        if c.get("/mainframe/state").json()["batch"]["status"] == "ABENDED":
            return
    raise AssertionError("batch never abended")


def test_workflow_drift_refuses_restart_until_release_hold(c):
    ops = {o["op"] for o in c.get("/mainframe/ops").json()}
    assert "release_hold" not in ops                       # baseline catalog unchanged
    assert "workflow" not in c.get("/mainframe/state").json()
    assert op(c, "mainframe", "release_hold")["accepted"] is False   # not an op before the drift

    assert chaos(c, "mainframe", "workflow_drift")["ok"]
    _abend(c)
    st = c.get("/mainframe/state").json()
    assert st["workflow"]["hold"] is True and st["workflow"]["version"] == 2
    cat = {o["op"]: o for o in c.get("/mainframe/ops").json()}
    assert cat["release_hold"]["before"] == "restart_batch" and cat["release_hold"]["destructive"] is False
    assert cat["restart_batch"]["requires"] == ["release_hold"]

    for rid in st["batch"]["poison_records"]:
        assert op(c, "mainframe", "quarantine_record", {"record_id": rid})["applied"]
    r = op(c, "mainframe", "restart_batch", {"job": "NIGHTBAT"})
    assert r["accepted"] is False and "issue release_hold before restart_batch" in r["message"]
    assert op(c, "mainframe", "release_hold", {"job": "NIGHTBAT"})["applied"] is True
    assert c.get("/mainframe/state").json()["workflow"]["hold"] is False
    assert op(c, "mainframe", "restart_batch", {"job": "NIGHTBAT"})["applied"] is True
    assert c.get("/mainframe/state").json()["batch"]["status"] == "OK"
