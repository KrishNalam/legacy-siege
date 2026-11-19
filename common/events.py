"""Write the Hive's visible chatter: thoughts (per reasoning step) and reporter-bus messages."""
from __future__ import annotations

from .db import col, now
from . import comms


def think(agent: str, role: str, text: str, incident: str | None = None, system: str | None = None,
          kind: str = "thought") -> None:
    col("thoughts").insert_one({
        "agent": agent, "role": role, "incident": incident, "system": system,
        "text": text[:280], "kind": kind, "ts": now(),
    })


def say(frm: str, to: str, kind: str, body: dict | str, *, incident=None, system=None,
        severity=None, count=1, processed=False, priority=None, source=None) -> None:
    doc = {"from": frm, "to": to, "kind": kind, "body": body, "ts": now()}
    if comms.enabled():
        doc.update(comms.metadata(source or frm, kind, body, incident=incident, system=system, severity=severity))
        doc.update(count=count, comms_processed=processed)
        if priority is not None:
            doc["priority"] = priority
    col("messages").insert_one(doc)
