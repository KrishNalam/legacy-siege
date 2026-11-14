"""Small HTTP helpers shared by the systems tests."""
import uuid


def tick(c, n=1):
    assert c.post(f"/_admin/tick?n={n}").status_code == 200


def op(c, sys, name, params=None, action_id=None):
    body = {"action_id": action_id or f"test-{uuid.uuid4().hex[:10]}", "params": params or {}}
    r = c.post(f"/{sys}/ops/{name}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def chaos(c, sys, fault, params=None):
    r = c.post(f"/{sys}/_chaos/{fault}", json={"params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


def status(c, sys):
    h = c.get(f"/{sys}/health").json()
    return h["status"], h["blames"]
