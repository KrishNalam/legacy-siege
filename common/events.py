"""Write the Hive's visible chatter: thoughts (per reasoning step) and waggle-bus messages."""
from __future__ import annotations

from .db import col, now


def think(agent: str, role: str, text: str, incident: str | None = None, system: str | None = None,
          kind: str = "thought") -> None:
    col("thoughts").insert_one({
        "agent": agent, "role": role, "incident": incident, "system": system,
        "text": text[:280], "kind": kind, "ts": now(),
    })


def say(frm: str, to: str, kind: str, body: dict | str) -> None:
    col("messages").insert_one({"from": frm, "to": to, "kind": kind, "body": body, "ts": now()})
