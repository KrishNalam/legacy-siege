"""LangSmith tracing switch (common/tracing.py): off = a pure no-op; on needs a key; hooks never raise.
Import checks run in a fresh subprocess so the parent's modules (langgraph pulls in langsmith) don't count."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

PROBE = """
import logging, os, sys
logging.basicConfig(level=logging.WARNING, format='LOG:%(message)s')
from common import tracing, llm
class C:  # stands in for an anthropic client
    pass
c = C()
assert tracing.enabled() is {expect}, tracing.enabled()
assert (tracing.wrap_anthropic(c) is c) is {same}
g = object()
assert (tracing.traced_graph(g, types.SimpleNamespace(role='r', id='r-1')) is g) is {same}
print('LS', 'langsmith' in sys.modules, os.environ.get('LANGSMITH_TRACING'), os.environ.get('LANGSMITH_PROJECT'))
"""


def probe(env: dict, expect: bool, same: bool) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("LANGSMITH", "LANGCHAIN"))}
    base["PYTHONPATH"] = str(ROOT)
    code = "import types\n" + PROBE.format(expect=expect, same=same)
    return subprocess.run([sys.executable, "-c", code], env={**base, **env}, cwd=ROOT, text=True,
                          capture_output=True, timeout=60)


@pytest.mark.parametrize("flag", ["", "0", "false", "off"])
def test_flag_off_imports_and_configures_nothing(flag):
    r = probe({"LANGSMITH_TRACING": flag, "LANGSMITH_API_KEY": "lsv2_dummy"}, expect=False, same=True)
    assert r.returncode == 0, r.stderr
    assert "LS False" in r.stdout  # langsmith never imported
    assert r.stdout.strip().endswith("None")  # no project configured
    assert "LOG:" not in r.stderr  # silent


def test_flag_on_without_key_stays_off_with_one_warning():
    r = probe({"LANGSMITH_TRACING": "1", "LANGSMITH_API_KEY": ""}, expect=False, same=True)
    assert r.returncode == 0, r.stderr
    assert "LS False false None" in r.stdout  # not imported; tracing explicitly disabled for LangChain too
    assert r.stderr.count("LOG:") == 1 and "LANGSMITH_API_KEY" in r.stderr


@pytest.fixture
def fresh(monkeypatch):
    from common import tracing

    for k in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGCHAIN_TRACING_V2"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tracing, "_state", None)
    monkeypatch.setattr(tracing, "_agent", {})
    monkeypatch.setattr(tracing, "_night_cache", (0.0, None))
    return tracing


def test_key_missing_logs_once(fresh, monkeypatch, caplog):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    with caplog.at_level(logging.WARNING, logger="common.tracing"):
        assert fresh.enabled() is False and fresh.enabled() is False
    assert len(caplog.records) == 1


def test_on_with_key_configures_short_timeouts_and_default_project(fresh, monkeypatch):
    import langsmith

    seen = {}
    monkeypatch.setattr(langsmith, "Client", lambda **kw: seen.update(kw) or "client")
    monkeypatch.setattr(langsmith, "configure", lambda **kw: seen.update(configured=kw))
    monkeypatch.setenv("LANGSMITH_TRACING", "1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_dummy")
    assert fresh.enabled() is True
    assert os.environ["LANGSMITH_TRACING"] == "true" and os.environ["LANGSMITH_PROJECT"] == "legacy-siege"
    assert seen["auto_batch_tracing"] is True and max(seen["timeout_ms"]) <= 5000
    assert seen["configured"] == {"client": "client"}


def test_setup_failure_never_raises(fresh, monkeypatch):
    import langsmith

    def boom(**_):
        raise RuntimeError("no network")

    monkeypatch.setattr(langsmith, "Client", boom)
    monkeypatch.setenv("LANGSMITH_TRACING", "1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_dummy")
    assert fresh.enabled() is False


def test_traced_graph_names_tags_and_metadata(fresh, monkeypatch):
    monkeypatch.setattr(fresh, "_state", True)
    monkeypatch.setattr(fresh, "_night", lambda: 2)
    calls = []

    class G:
        def invoke(self, input=None, config=None, **kw):
            calls.append((input, config, kw))
            return "ok"

        def get_state(self, cfg):
            return "state"

    fresh.bind("worker", "worker-unix-1")
    ag = types.SimpleNamespace(role="worker", id="worker-unix-1")
    tg = fresh.traced_graph(G(), ag)
    assert tg.get_state({}) == "state"  # passthrough
    assert tg.invoke({"incident": "INC-0001"}, {"configurable": {"thread_id": "t"}}, durability="sync") == "ok"
    _, cfg, kw = calls[0]
    assert cfg["run_name"] == "worker:worker-unix-1" and cfg["configurable"] == {"thread_id": "t"}
    assert set(cfg["tags"]) >= {"worker", "worker-unix-1"}
    assert cfg["metadata"] == {"role": "worker", "agent_id": "worker-unix-1", "incident_id": "INC-0001",
                               "night": 2}
    assert kw == {"durability": "sync"}
    tg.invoke(None, None)  # resume path: no input, no config
    assert calls[1][1]["metadata"] == {"role": "worker", "agent_id": "worker-unix-1", "night": 2}
    assert "key" not in str(calls[0][1]["metadata"]).lower()


def test_hook_errors_never_reach_the_agent(fresh, monkeypatch):
    monkeypatch.setattr(fresh, "_state", True)
    monkeypatch.setattr(fresh, "_meta", lambda *_: 1 / 0)

    class G:
        def invoke(self, input=None, config=None, **kw):
            return ("ran", config)

    tg = fresh.traced_graph(G(), types.SimpleNamespace(role="r", id="r-1"))
    assert tg.invoke({"incident": "X"}, {"a": 1}) == ("ran", {"a": 1})  # falls back to the plain call
    client = object()
    import langsmith.wrappers as w

    monkeypatch.setattr(w, "wrap_anthropic", lambda *a, **k: 1 / 0)
    assert fresh.wrap_anthropic(client) is client


def test_llm_complete_returns_none_without_key_and_untouched_by_tracing(monkeypatch):
    from common import config, llm

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    assert llm.complete("s", "p") is None
