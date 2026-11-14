"""Embedding provider selection and the Voyage -> local-hash fallback (no network, no MongoDB)."""
import sys
import types

import pytest

from common import config, embed


class _Boom:
    calls = 0

    def __init__(self, **kw):
        self.kw = kw

    def embed(self, *a, **k):
        _Boom.calls += 1
        raise TimeoutError("voyage timed out")


class _Ok:
    seen: dict = {}

    def __init__(self, **kw):
        _Ok.seen["client"] = kw

    def embed(self, texts, **k):
        _Ok.seen["embed"] = k
        return types.SimpleNamespace(embeddings=[[0.5] * config.EMBED_DIM])


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "VOYAGE_API_KEY", "test-key")
    monkeypatch.setattr(embed, "_voyage_down_until", 0.0)
    _Boom.calls = 0


def _fake(monkeypatch, cls):
    monkeypatch.setitem(sys.modules, "voyageai", types.SimpleNamespace(Client=cls))


def test_voyage_call_is_bounded_and_dimensioned(monkeypatch):
    _fake(monkeypatch, _Ok)
    vec, model = embed.embed_with_model("hello", "query")
    assert model == config.VOYAGE_MODEL and len(vec) == config.EMBED_DIM
    assert _Ok.seen["embed"]["input_type"] == "query"
    assert _Ok.seen["embed"]["output_dimension"] == config.EMBED_DIM
    assert _Ok.seen["client"]["timeout"] == config.EMBED_TIMEOUT_S


def test_error_falls_back_to_local_and_backs_off(monkeypatch):
    _fake(monkeypatch, _Boom)
    vec, model = embed.embed_with_model("hello")
    assert model == embed.LOCAL_MODEL and len(vec) == config.EMBED_DIM
    embed.embed_with_model("again")
    assert _Boom.calls == 1  # second call skipped Voyage during the cooldown


def test_wrong_dimension_is_rejected(monkeypatch):
    class Short(_Ok):
        def embed(self, texts, **k):
            return types.SimpleNamespace(embeddings=[[0.1] * 8])

    _fake(monkeypatch, Short)
    assert embed.embed_with_model("x")[1] == embed.LOCAL_MODEL


def test_no_key_uses_local(monkeypatch):
    monkeypatch.setattr(config, "VOYAGE_API_KEY", None)
    assert embed.provider() == "local-hash"
    assert embed.embed_with_model("x")[1] == embed.LOCAL_MODEL


def test_hung_voyage_is_bounded(monkeypatch):
    import time

    class Hang(_Ok):
        def embed(self, texts, **k):
            time.sleep(3)

    _fake(monkeypatch, Hang)
    monkeypatch.setattr(config, "EMBED_TIMEOUT_S", 0.2)
    t = time.monotonic()
    assert embed.embed_with_model("x")[1] == embed.LOCAL_MODEL
    assert time.monotonic() - t < 1.5
