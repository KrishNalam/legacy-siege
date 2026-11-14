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


def complete_json(system: str, prompt: str, schema: dict, max_tokens: int = 900,
                  timeout: float | None = None) -> dict | None:
    """One structured-output call (JSON matching `schema`), or None on no key / timeout / refusal /
    truncation / bad JSON. No retries: callers use this off the critical path and must not wait long."""
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        import json

        import anthropic

        c = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=timeout or config.LLM_TIMEOUT_S,
                                max_retries=0)
        try:
            msg = c.messages.create(
                model=config.LLM_MODEL,
                max_tokens=max_tokens,
                thinking={"type": "disabled"},
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        finally:
            c.close()
        if msg.stop_reason != "end_turn":  # refusal or max_tokens: never trust a partial answer
            return None
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        out = json.loads(text) if text else None
        return out if isinstance(out, dict) else None
    except Exception:
        return None
