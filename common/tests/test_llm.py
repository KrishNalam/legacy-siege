"""Narration always releases its HTTP client, including provider failures."""
import sys
from types import SimpleNamespace

import pytest

from common import config, llm


@pytest.mark.parametrize("fails", [False, True])
def test_narration_closes_client(monkeypatch, fails):
    closed = []

    class Client:
        def __init__(self, **kwargs):
            self.messages = self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append(True)

        def create(self, **kwargs):
            if fails:
                raise TimeoutError("provider unavailable")
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="ready")])

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-only")
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=Client))
    assert llm.complete("system", "prompt") == (None if fails else "ready")
    assert closed == [True]
