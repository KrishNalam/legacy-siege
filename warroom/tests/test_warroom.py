"""War-room server: BSON encoding, change-stream fan-out over /ws, resume tokens, and the HTTP API."""
from __future__ import annotations

import time
from datetime import timedelta

import pytest
from bson import Binary, ObjectId
from fastapi.testclient import TestClient

from common.db import now, swarm, world
from warroom.bson_json import slim, to_json
from warroom.stream import to_message


# ------------------------------------------------------------------ pure helpers
def test_to_json_strips_embeddings_and_encodes_bson():
    oid = ObjectId()
    doc = {"_id": oid, "ts": now(), "embedding": [0.1] * 1024, "embedding_model": "local-hash-v1",
           "blob": Binary(b"\x00" * 10), "nested": [{"embedding": [1.0, 2.0]}]}
    out = to_json(doc)
    assert out["_id"] == str(oid)
    assert out["ts"].endswith("Z")
    assert out["embedding"] == {"dims": 1024, "model": "local-hash-v1", "stripped": True}
    assert out["nested"][0]["embedding"]["dims"] == 2
    assert out["blob"] == {"$binary": "10 bytes"}


def test_checkpoint_docs_are_slimmed_to_ids():
    out = slim("checkpoints", {"_id": ObjectId(), "thread_id": "INC-1-t3", "checkpoint_id": "abc",
                               "checkpoint": b"\x00" * 999, "metadata": {"big": "x"}})
    assert set(out) == {"_id", "thread_id", "checkpoint_id"}


def _change(op, coll, fields=None, doc=None, db="swarm_test_warroom"):
    ch = {"operationType": op, "ns": {"db": db, "coll": coll}, "documentKey": {"_id": "a1"}}
    if fields is not None:
        ch["updateDescription"] = {"updatedFields": {f: 1 for f in fields}, "removedFields": []}
    if doc is not None:
        ch["fullDocument"] = doc
    return ch


def test_heartbeats_are_aggregated_not_forwarded():
    msg, coll, quiet = to_message(_change("update", "agents", ["last_seen"], {"_id": "a1"}))
    assert msg is None and quiet and coll == "agents"
    msg, _, quiet = to_message(_change("update", "agents", ["last_seen", "target"], {"_id": "a1", "target": "unix"}))
    assert msg and not quiet and msg["fields"] == ["last_seen", "target"] and msg["db"] == "swarm"


def test_count_only_and_delete_messages():
    assert to_message(_change("insert", "system_state", doc={"_id": "x"}))[0] is None
    msg, _, _ = to_message(_change("delete", "thoughts"))
    assert msg["op"] == "delete" and msg["doc"] is None and msg["key"] == "a1"


# ------------------------------------------------------------------ live server
@pytest.fixture()
def app_client(clean):
    from warroom.server import app
    with TestClient(app) as c:
        yield c


def _recv_until(ws, pred, limit=40):
    for _ in range(limit):
        m = ws.receive_json()
        if pred(m):
            return m
    raise AssertionError("expected message never arrived")


def test_ws_snapshot_then_change_stream(app_client):
    swarm().agents.insert_one({"_id": "scout-1", "role": "scout", "status": "idle", "last_seen": now()})
    with app_client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        assert [a["_id"] for a in snap["agents"]] == ["scout-1"]
        assert snap["meta"]["dbs"] == {"world": "world_test_warroom", "swarm": "swarm_test_warroom"}
        swarm().agents.update_one({"_id": "scout-1"}, {"$set": {"last_seen": now()}})  # heartbeat: aggregated
        swarm().thoughts.insert_one({"agent": "scout-1", "role": "scout", "text": "hello", "kind": "thought", "ts": now()})
        seen = []
        m = _recv_until(ws, lambda m: seen.append(m) or (m["type"] == "change" and m["coll"] == "thoughts"))
        assert m["op"] == "insert" and m["doc"]["text"] == "hello"
        # the agent insert may replay (buffered before the snapshot) but the heartbeat update is never forwarded
        assert not [x for x in seen if x.get("coll") == "agents" and x.get("op") == "update"]
        tick = _recv_until(ws, lambda m: m["type"] == "tick")
        assert tick["counts"]["thoughts"] == 1 and "ops_per_sec" in tick


def test_resume_token_persisted_and_runs_mirrored(app_client):
    with app_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        world().runs.insert_one({"_id": "RUN-0007", "scenario": "bank-night-2", "round": 2, "started_at": now()})
        _recv_until(ws, lambda m: m["type"] == "change" and m["coll"] == "stats" and m["key"] == "current")
    deadline = time.time() + 5
    while time.time() < deadline and not swarm().stats.find_one({"_id": "stream_tokens"}):
        time.sleep(0.2)
    assert swarm().stats.find_one({"_id": "stream_tokens"})["token"]
    assert swarm().stats.find_one({"_id": "current"})["run_id"] == "RUN-0007"


def test_approvals_decide_only_pending(app_client):
    swarm().approvals.insert_one({"_id": "APR-0001", "kind": "step", "status": "pending", "ts": now()})
    r = app_client.post("/api/approvals/APR-0001", json={"decision": "approved"})
    assert r.status_code == 200
    doc = swarm().approvals.find_one({"_id": "APR-0001"})
    assert doc["status"] == "approved" and doc["decided_by"] == "beekeeper" and doc["decided_at"]
    assert app_client.post("/api/approvals/APR-0001", json={"decision": "denied"}).status_code == 409
    assert app_client.post("/api/approvals/APR-9999", json={"decision": "denied"}).status_code == 404
    assert app_client.post("/api/approvals/APR-0001", json={"decision": "maybe"}).status_code == 400


def test_scorecard_aggregates_rounds(app_client):
    t = now()
    swarm().incidents.insert_many([
        {"_id": "INC-1", "status": "resolved", "system": "portal", "opened_at": t, "round": 1, "run_id": "RUN-0001",
         "metrics": {"ttr_s": 90.0, "diagnosis_s": 30, "hops": 3, "attempts": 5}, "recall": {"used": False, "hits": []}},
        {"_id": "INC-2", "status": "resolved", "system": "portal", "opened_at": t, "round": 2, "run_id": "RUN-0002",
         "metrics": {"ttr_s": 30.0, "diagnosis_s": 2, "hops": 0, "attempts": 4},
         "recall": {"used": True, "hits": [{"playbook_id": "PB-1", "score": 0.93}]}},
    ])
    world().chaos_events.insert_many([
        {"_id": "CHX-1", "fault": "batch_abort", "run_id": "RUN-0001", "round": 1, "manual": False, "ts": t},
        {"_id": "CHX-2", "fault": "agent_kill", "run_id": "RUN-0001", "round": 1, "manual": True, "ts": t + timedelta(seconds=1)},
    ])
    r = app_client.get("/api/scorecard").json()
    by_round = {x["round"]: x for x in r["rounds"]}
    assert by_round[1]["avg_ttr_s"] == 90 and by_round[2]["avg_hops"] == 0
    assert by_round[2]["recall_used"] == 1 and by_round[2]["best_recall_score"] == 0.93
    assert r["speedup"] == 3.0
    assert r["runs"][0]["events"] == 2 and r["runs"][0]["incidents"] == 1 and r["runs"][0]["manual"] == 1


def test_doc_lookup_by_id_and_alt_key(app_client):
    swarm().ledger.insert_one({"action_id": "INC-1-s1-a1", "incident": "INC-1", "agent": "w", "op": "x",
                               "status": "sent", "ts": now()})
    r = app_client.get("/api/doc/swarm/ledger/INC-1-s1-a1").json()
    assert r["matched_on"] == "action_id" and r["doc"]["status"] == "sent"
    swarm().playbooks.insert_one({"_id": "PB-1", "embedding": [0.5] * 8, "embedding_model": "local-hash-v1"})
    r = app_client.get("/api/doc/swarm/playbooks/PB-1").json()
    assert r["doc"]["embedding"]["dims"] == 8
    assert app_client.get("/api/doc/swarm/nope/1").status_code == 404
    assert app_client.get("/api/doc/admin/system.users/1").status_code == 404


def test_atlas_lists_collections(app_client):
    swarm().playbooks.insert_one({"_id": "PB-1", "embedding": [0.5] * 8, "embedding_model": "m"})
    r = app_client.get("/api/atlas").json()
    pb = next(c for c in r["collections"] if c["coll"] == "playbooks")
    assert pb["count"] == 1 and pb["sample"]["embedding"]["stripped"]


def test_reset_next_round_keeps_memory_full_wipes(app_client):
    swarm().playbooks.insert_one({"_id": "PB-1", "embedding_model": "m"})
    swarm().incidents.insert_one({"_id": "INC-1", "status": "fixing", "system": "portal", "opened_at": now(), "active": True})
    swarm().approvals.insert_one({"_id": "APR-1", "status": "pending", "ts": now()})
    swarm().tasks.insert_one({"_id": "INC-1-t1", "incident": "INC-1"})
    r = app_client.post("/api/reset", json={"mode": "next_round"}).json()
    assert r["ok"] and r["closed_incidents"] == 1 and "unreachable" in str(r["systems"])
    assert swarm().playbooks.count_documents({}) == 1
    assert swarm().incidents.find_one({"_id": "INC-1"})["active"] is False
    assert swarm().approvals.find_one({"_id": "APR-1"})["status"] == "denied"
    assert swarm().tasks.count_documents({}) == 0
    assert app_client.post("/api/reset", json={"mode": "full"}).json()["ok"]
    assert swarm().playbooks.count_documents({}) == 0 and swarm().incidents.count_documents({}) == 0
    assert app_client.post("/api/reset", json={"mode": "bogus"}).status_code == 400


def test_chaos_controls(app_client):
    pytest.importorskip("chaos.runner")
    assert app_client.post("/api/chaos/batch_abort", json={"target": "portal"}).status_code == 400
    r = app_client.post("/api/scenario/bank-night-2")
    assert r.status_code == 200, r.text
    body = r.json()
    cur = swarm().stats.find_one({"_id": "current"})
    assert cur["run_id"] == body["run_id"] and cur["round"] == 1
    assert app_client.post("/api/scenario/no-such-night").status_code == 404


def test_static_page_served(app_client):
    r = app_client.get("/")
    assert r.status_code == 200 and "LEGACY SIEGE" in r.text
    assert app_client.get("/app.js").status_code == 200
    assert app_client.get("/js/stage.js").status_code == 200


def test_flow_view_is_default_and_classic_is_kept(app_client):
    stage = app_client.get("/")
    assert stage.status_code == 200 and "flow.js" in stage.text and "LEGACY SIEGE" in stage.text
    for url in ("/?classic=1", "/classic"):
        r = app_client.get(url)
        assert r.status_code == 200 and 'src="app.js"' in r.text, url  # the classic war room, unchanged
    assert "flow.js" in app_client.get("/?classic=0").text
    for asset in ("/flow.js", "/flow.css", "/flow.html"):
        assert app_client.get(asset).status_code == 200, asset
