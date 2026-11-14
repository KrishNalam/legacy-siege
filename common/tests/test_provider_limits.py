"""Persistent SDK stalls must preserve fallback without accumulating live calls."""
import threading

from common import config, embed, llm


def test_llm_timeout_keeps_slot_until_provider_exits(monkeypatch):
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(llm, "_provider_slots", slots)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_PROVIDER", "stalled-test")
    calls = []

    def provider(*args):
        calls.append(1)
        entered.set()
        try:
            release.wait(5)
            return '{"ok":true}', 'ok'
        finally:
            exited.set()

    monkeypatch.setitem(llm.PROVIDERS, "stalled-test", provider)
    try:
        assert llm.guarded_json("test", "system", "prompt", {}, timeout=0.5)[0] is None
        assert entered.is_set()
        assert llm.guarded_json("test", "system", "prompt", {}, timeout=0.5)[0] is None
        assert len(calls) == 1
    finally:
        release.set()
        assert exited.wait(2)
    # Acquire waits until the wrapper's finally has released its slot too.
    assert slots.acquire(timeout=2)
    slots.release()
    assert llm.guarded_json("test", "system", "prompt", {}, timeout=0.5)[0] == {"ok": True}


def test_voyage_timeout_keeps_slot_until_provider_exits(monkeypatch):
    import sys
    from types import SimpleNamespace
    release, exited = threading.Event(), threading.Event()
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(embed, "_voyage_slots", slots)
    monkeypatch.setattr(config, "VOYAGE_API_KEY", "test-key")
    monkeypatch.setattr(config, "EMBED_TIMEOUT_S", 0.02)
    monkeypatch.setattr(embed, "_voyage_down_until", 0)
    calls = []

    class Provider:
        def __init__(self, **kwargs):
            pass

        def embed(self, *args, **kwargs):
            calls.append(1)
            try:
                release.wait(5)
                return SimpleNamespace(embeddings=[[0.5] * config.EMBED_DIM])
            finally:
                exited.set()

    monkeypatch.setitem(sys.modules, "voyageai", SimpleNamespace(Client=Provider))
    try:
        assert embed.embed_with_model("first")[1] == embed.LOCAL_MODEL
        monkeypatch.setattr(embed, "_voyage_down_until", 0)  # even after cooldown
        assert embed.embed_with_model("second")[1] == embed.LOCAL_MODEL
        assert len(calls) == 1
    finally:
        release.set()
        assert exited.wait(2)
    assert slots.acquire(timeout=2)
    slots.release()
    monkeypatch.setattr(embed, "_voyage_down_until", 0)
    assert embed.embed_with_model("recovered")[1] == config.VOYAGE_MODEL
