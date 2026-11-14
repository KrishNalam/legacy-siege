"""A tiny in-memory stand-in for systems.app (CONTRACTS §2 subset) to exercise the app replay."""
from fastapi import FastAPI

from systems import mainframe

app = FastAPI()
S: dict = {}
GOOD = ("0000004211" + "+00000001234").ljust(80)
POISON = ("0000004211" + "00000012A4  ").ljust(80)


def _reset():
    S.update(queue=[GOOD], status="OK", poison=[], quarantined=[], logs=[], rejected=[])


def _run():
    for i, r in enumerate(S["queue"]):
        if not mainframe.validate_record(r):
            S["rejected"].append(i)
            S["logs"].append({"ts": "t", "level": "WARN", "code": "NB100W", "line": f"NIGHTBAT REJECTED RECORD {i}"})
            continue
        try:
            mainframe.parse_amount(r[mainframe.AMOUNT_FIELD])
        except mainframe.Abend:
            S["status"], S["poison"] = "ABENDED", [f"REC-{i}"]
            S["logs"].append({"ts": "t", "level": "ERROR", "code": "IEF450I", "line": "IEF450I NIGHTBAT ABEND=S0C7"})
            return
    S["status"], S["queue"] = "OK", []


_reset()


@app.post("/_admin/reset")
def reset():
    _reset()
    return {"ok": True}


@app.post("/mainframe/_chaos/batch_abort")
def batch_abort(body: dict):
    S["queue"].append(POISON)
    _run()
    return {"ok": True, "effect": "poison injected"}


@app.post("/mainframe/ops/{op}")
def op(op: str, body: dict):
    _run()
    return {"accepted": True, "applied": S["status"] == "OK", "message": S["status"], "action_id": body["action_id"]}


@app.get("/mainframe/state")
def state():
    return {"batch": {"job": "NIGHTBAT", "status": S["status"], "poison_records": S["poison"],
                      "quarantined": S["quarantined"]}, "validation_enabled": True, "version": 1}


@app.get("/mainframe/logs")
def logs(since: str = "", limit: int = 50):
    return {"system": "mainframe", "lines": S["logs"][-limit:]}
