"""Optional LLM. Everything must work when this returns None (no key / timeout / error)."""
from __future__ import annotations

from . import config


def llm_available() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def complete(system: str, prompt: str, max_tokens: int = 200) -> str | None:
    """One short completion, or None. Current Claude models reject sampling params (temperature etc.),
    so determinism comes from tight prompts; thinking is off and effort low to keep it ~1 s."""
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic

        c = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=config.LLM_TIMEOUT_S, max_retries=1)
        msg = c.messages.create(
            model=config.LLM_MODEL,
            max_tokens=max_tokens,
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        if msg.stop_reason == "refusal":
            return None
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip() or None
    except Exception:
        return None
