"""Optional LangSmith tracing, behind ONE switch and a complete no-op without it.

    LANGSMITH_TRACING=1        the switch (1/true/yes/on)
    LANGSMITH_API_KEY=...      required; without it tracing stays off (one warning line)
    LANGSMITH_PROJECT=...      optional, default "legacy-siege"

Off (flag unset/false, or key missing): ``langsmith`` is never imported, nothing is configured, no network
calls, and the helpers below return their argument unchanged. On: LangGraph runs are traced per agent as
``<role>:<agent_id>`` with tags/metadata {role, agent_id, incident_id, night}, and the direct Anthropic calls
in ``common.llm`` become traced LLM runs (nested under the graph run that made them). Sends happen on
LangSmith's background batching thread with short timeouts, and every hook here swallows its own errors:
tracing can never crash or block an agent. Only ids go into metadata — never keys.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from . import config  # noqa: F401  (importing it loads .env, so the switch can live there)

log = logging.getLogger("common.tracing")

_TRUE = {"1", "true", "yes", "on"}
DEFAULT_PROJECT = "legacy-siege"
_CONNECT_TIMEOUT_MS, _READ_TIMEOUT_MS = 2000, 5000

_lock = threading.Lock()
_state: bool | None = None   # None = not decided yet
_agent: dict[str, str] = {}  # {role, agent_id} of this process, set by bind()
_night_cache: tuple[float, int | None] = (0.0, None)


def _flag_on() -> bool:
    return os.environ.get("LANGSMITH_TRACING", "").strip().lower() in _TRUE


def enabled() -> bool:
    """Decide once per process whether tracing is on (and configure LangSmith if so)."""
    global _state
    if _state is not None:
        return _state
    with _lock:
        if _state is None:
            _state = _setup()
    return _state


def _setup() -> bool:
    if not _flag_on():
        return False
    if not (os.environ.get("LANGSMITH_API_KEY") or "").strip():
        log.warning("LANGSMITH_TRACING is set but LANGSMITH_API_KEY is missing; tracing stays off")
        # LangChain/LangGraph read these themselves: make sure they cannot trace (and fail) without a key.
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        return False
    try:
        os.environ["LANGSMITH_TRACING"] = "true"   # langsmith only accepts the literal "true"
        os.environ.pop("LANGCHAIN_TRACING_V2", None)
        if not (os.environ.get("LANGSMITH_PROJECT") or "").strip():
            os.environ["LANGSMITH_PROJECT"] = DEFAULT_PROJECT
        import langsmith

        langsmith.configure(client=langsmith.Client(
            timeout_ms=(_CONNECT_TIMEOUT_MS, _READ_TIMEOUT_MS), auto_batch_tracing=True))
        log.info("LangSmith tracing on (project %s)", os.environ["LANGSMITH_PROJECT"])
        return True
    except Exception as e:  # not installed / bad config: run untraced
        log.warning("LangSmith tracing could not start (%s: %s); tracing stays off", type(e).__name__, e)
        os.environ["LANGSMITH_TRACING"] = "false"
        return False


def bind(role: str, agent_id: str) -> None:
    """Remember which agent this process is (used for run names/metadata of direct LLM calls)."""
    _agent.update(role=role, agent_id=agent_id)


def _night() -> int | None:
    """The current demo night/round (swarm.stats 'current'), cached for a few seconds."""
    global _night_cache
    at, val = _night_cache
    if time.monotonic() - at < 5:
        return val
    try:
        from .db import col

        doc = col("stats").find_one({"_id": "current"}, {"round": 1}) or {}
        val = doc.get("round")
        val = int(val) if val is not None else None
    except Exception:
        val = None
    _night_cache = (time.monotonic(), val)
    return val


def _meta(incident: str | None = None) -> dict:
    m = {"role": _agent.get("role"), "agent_id": _agent.get("agent_id"), "incident_id": incident,
         "night": _night()}
    return {k: v for k, v in m.items() if v is not None}


class _TracedGraph:
    """Wraps a compiled graph so ``invoke`` carries run name + tags + metadata; everything else
    (get_state, ...) passes straight through."""

    def __init__(self, graph, role: str, agent_id: str, name: str):
        self._graph, self._role, self._agent_id, self._name = graph, role, agent_id, name

    def __getattr__(self, item):
        return getattr(self._graph, item)

    def invoke(self, input=None, config=None, **kwargs):
        try:
            incident = input.get("incident") if isinstance(input, dict) else None
            meta = {**_meta(incident if isinstance(incident, str) else None),
                    "role": self._role, "agent_id": self._agent_id}
            cfg = dict(config or {})
            cfg.setdefault("run_name", self._name)
            cfg["tags"] = list(dict.fromkeys([*(cfg.get("tags") or []), self._role, self._agent_id]))
            cfg["metadata"] = {**(cfg.get("metadata") or {}), **meta}
        except Exception:
            cfg = config
        return self._graph.invoke(input, cfg, **kwargs)


def traced_graph(graph, agent, name: str | None = None):
    """``graph = traced_graph(graph, agent)`` in a role's run(): the same graph when tracing is off."""
    if not enabled():
        return graph
    try:
        return _TracedGraph(graph, agent.role, agent.id, name or f"{agent.role}:{agent.id}")
    except Exception:
        return graph


def wrap_anthropic(client):
    """Make an ``anthropic`` client's calls traced LLM runs (same client object back); no-op when off."""
    if not enabled():
        return client
    try:
        from langsmith.wrappers import wrap_anthropic as _wrap

        tags = ["llm", *[v for v in (_agent.get("role"), _agent.get("agent_id")) if v]]
        return _wrap(client, tracing_extra={"tags": tags, "metadata": _meta()}, chat_name="claude")
    except Exception:
        return client


def flush(timeout: float = 2.0) -> None:
    """Best-effort send of buffered runs on clean shutdown; never waits longer than ``timeout``."""
    if not _state:
        return
    try:
        from langsmith import run_trees

        c = getattr(run_trees, "_CLIENT", None)
        if c is not None:
            t = threading.Thread(target=c.flush, daemon=True)
            t.start()
            t.join(timeout)
    except Exception:
        pass
