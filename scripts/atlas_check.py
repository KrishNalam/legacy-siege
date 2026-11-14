"""Check that the MongoDB deployment in MONGODB_URI supports everything Legacy Siege needs.

    MONGODB_URI='mongodb+srv://...' .venv/bin/python -m scripts.atlas_check

Checks: connectivity, replica set (transactions), multi-document transaction, change stream,
TTL + partial unique index, $jsonSchema validator, Atlas Vector Search index build + query.
Uses a throwaway database `legacy_siege_check` and drops it afterwards.
"""
from __future__ import annotations

import threading
import time

from pymongo import MongoClient
from pymongo.errors import PyMongoError, WriteError
from pymongo.operations import SearchIndexModel

from common import config

DB = "legacy_siege_check"


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def record(name: str, fn) -> None:
        t0 = time.monotonic()
        try:
            detail = fn() or ""
            results.append((name, True, f"{detail} ({time.monotonic() - t0:.1f}s)"))
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {e}"[:200]))

    c = MongoClient(config.MONGODB_URI, serverSelectionTimeoutMS=8000, tz_aware=True)
    db = c[DB]

    def connect():
        info = c.server_info()
        hello = c.admin.command("hello")
        rs = hello.get("setName") or ("mongos" if hello.get("msg") == "isdbgrid" else None)
        if not rs:
            raise RuntimeError("not a replica set: transactions and change streams need one")
        return f"MongoDB {info['version']}, {rs}"

    def transaction():
        db.drop_collection("tx")
        db.create_collection("tx")
        with c.start_session() as s, s.start_transaction():
            db.tx.insert_one({"a": 1}, session=s)
            db.tx.insert_one({"b": 2}, session=s)
        return f"{db.tx.count_documents({})} docs committed atomically"

    def change_stream():
        got: list[str] = []

        def watch():
            with db.cs.watch(max_await_time_ms=3000) as stream:
                for ch in stream:
                    got.append(ch["operationType"])
                    return

        t = threading.Thread(target=watch, daemon=True)
        t.start()
        time.sleep(1.5)
        db.cs.insert_one({"x": 1})
        t.join(8)
        if not got:
            raise RuntimeError("no change event received")
        return f"received {got[0]}"

    def indexes():
        db.idx.create_index("ts", expireAfterSeconds=60)
        db.idx.create_index("key", unique=True, partialFilterExpression={"active": True})
        db.idx.insert_one({"key": "k", "active": True})
        db.idx.insert_one({"key": "k", "active": False})  # allowed: outside the partial filter
        try:
            db.idx.insert_one({"key": "k", "active": True})
        except PyMongoError:
            return "TTL + partial unique index ok"
        raise RuntimeError("partial unique index did not reject a duplicate active key")

    def validator():
        db.create_collection("val", validator={"$jsonSchema": {"required": ["status"], "properties": {"status": {"enum": ["ok"]}}}})
        try:
            db.val.insert_one({"status": "bogus"})
        except WriteError:
            return "rejects invalid docs"
        raise RuntimeError("validator accepted an invalid doc")

    def vector_search():
        db.vec.insert_many([
            {"sys": "mainframe", "embedding": [1.0, 0.0, 0.0, 0.0]},
            {"sys": "unix", "embedding": [0.0, 1.0, 0.0, 0.0]},
        ])
        db.vec.create_search_index(SearchIndexModel(name="v", type="vectorSearch", definition={"fields": [
            {"type": "vector", "path": "embedding", "numDimensions": 4, "similarity": "cosine"},
            {"type": "filter", "path": "sys"}]}))
        for _ in range(120):
            idx = list(db.vec.list_search_indexes("v"))
            if idx and idx[0].get("queryable"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("vector index never became queryable (120s)")
        for _ in range(30):  # index is queryable before the docs are synced into it
            hits = list(db.vec.aggregate([{"$vectorSearch": {"index": "v", "path": "embedding",
                        "queryVector": [0.9, 0.1, 0, 0], "numCandidates": 10, "limit": 1, "filter": {"sys": "mainframe"}}}]))
            if hits:
                return "index built, filtered query returned a hit"
            time.sleep(1)
        raise RuntimeError("vector query returned nothing")

    record("connect / replica set", connect)
    record("multi-document transaction", transaction)
    record("change stream", change_stream)
    record("TTL + partial unique index", indexes)
    record("$jsonSchema validator", validator)
    record("Atlas Vector Search", vector_search)
    try:
        c.drop_database(DB)
    except PyMongoError:
        pass

    host = config.MONGODB_URI.split("://", 1)[-1].split("@")[-1].split("/")[0].split("?")[0]
    print(f"\nLegacy Siege readiness check for {host}\n")
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:30s} {detail}")
    failed = [r for r in results if not r[1]]
    print(f"\n{'ALL GOOD' if not failed else f'{len(failed)} check(s) failed'}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
