"""Thin client for the legacy systems' HTTP API (CONTRACTS §2). The swarm's only window on the world."""
from __future__ import annotations

import time
from typing import Any

import httpx

from common import config


class SystemsUnavailable(RuntimeError):
    pass


class Systems:
    def __init__(self, base_url: str | None = None, timeout: float = 5.0):
        self._c = httpx.Client(base_url=base_url or config.SYSTEMS_URL, timeout=timeout)

    def _get(self, path: str, **params) -> Any:
        r = self._c.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    def health(self, sys: str) -> dict:
        return self._get(f"/{sys}/health")

    def metrics(self, sys: str) -> dict:
        return self._get(f"/{sys}/metrics")

    def logs(self, sys: str, limit: int = 50, since: str | None = None) -> list[dict]:
        if since:
            try:
                return self._get(f"/{sys}/logs", limit=limit, since=since).get("lines", [])
            except httpx.HTTPStatusError:
                pass  # a system that can't parse `since` still gets read
        return self._get(f"/{sys}/logs", limit=limit).get("lines", [])

    def state(self, sys: str) -> dict:
        return self._get(f"/{sys}/state")

    def ops(self, sys: str) -> list[dict]:
        return self._get(f"/{sys}/ops")

    def probe(self, sys: str, vuln_id: str) -> dict:
        r = self._c.post(f"/{sys}/security/probe/{vuln_id}")
        r.raise_for_status()
        return r.json()

    def post_op(self, sys: str, op: str, action_id: str, params: dict, retries: int = 3) -> dict:
        """POST an op. Transport retries reuse the SAME action_id, so the system returns its stored
        receipt instead of applying twice."""
        last: Exception | None = None
        for i in range(retries):
            try:
                r = self._c.post(f"/{sys}/ops/{op}", json={"action_id": action_id, "params": params})
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last = e
                time.sleep(0.3 * (i + 1))
        raise SystemsUnavailable(f"{sys}/{op} {action_id}: {last}")

    def version(self, sys: str) -> int:
        return int(self.health(sys).get("version", 1))
