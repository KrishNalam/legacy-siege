"""Embeddings for playbook memory ("honey").

Voyage AI when VOYAGE_API_KEY is set; otherwise a deterministic local hashing embedding
(token + bigram feature hashing, L2-normalised) with the same dimension so the Atlas
Vector Search index works either way. Switching providers requires re-embedding playbooks
(`python -m scripts.setup_db --reset` wipes them).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time

from . import config

_TOKEN = re.compile(r"[a-z0-9]+")


def provider() -> str:
    return "voyage" if config.VOYAGE_API_KEY else "local-hash"


def _local(text: str) -> list[float]:
    dim = config.EMBED_DIM
    vec = [0.0] * dim
    toks = _TOKEN.findall(text.lower())
    feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
    for f in feats:
        h = hashlib.blake2b(f.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "little") % dim
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


LOCAL_MODEL = "local-hash-v1"
_voyage_slots = threading.BoundedSemaphore(2)
_voyage_down_until = 0.0  # monotonic time before which we don't even try Voyage (set after a failure)


def _voyage(text: str, input_type: str) -> list[float]:
    """One Voyage call, hard-bounded to EMBED_TIMEOUT_S of wall clock (the SDK's own timeout/retry handling
    can take 3x longer, e.g. on an unreachable host). A straggler thread is a daemon and just finishes later."""
    import voyageai

    if not _voyage_slots.acquire(blocking=False):
        raise TimeoutError("voyage provider still busy")
    box: dict = {}

    def run() -> None:
        try:
            vo = voyageai.Client(api_key=config.VOYAGE_API_KEY, timeout=config.EMBED_TIMEOUT_S, max_retries=0)
            r = vo.embed([text], model=config.VOYAGE_MODEL, input_type=input_type, output_dimension=config.EMBED_DIM)
            box["vec"] = list(r.embeddings[0])
        except BaseException as e:  # noqa: BLE001 - reported to the caller below
            box["err"] = e
        finally:
            _voyage_slots.release()

    t = threading.Thread(target=run, daemon=True, name="voyage-embed")
    try:
        t.start()
    except BaseException:
        _voyage_slots.release()
        raise
    t.join(config.EMBED_TIMEOUT_S)
    if t.is_alive():
        raise TimeoutError(f"voyage embed exceeded {config.EMBED_TIMEOUT_S}s")
    if "err" in box:
        raise box["err"]
    if len(box["vec"]) != config.EMBED_DIM:
        raise ValueError(f"voyage returned {len(box['vec'])}-d vector, index expects {config.EMBED_DIM}")
    return box["vec"]


def _trace(input_type: str, ok: bool, t0: float, err: str = "") -> None:
    """Opt-in call log (EMBED_TRACE=/path/file.jsonl): one line per real Voyage call, no text, no secrets.
    Used to count calls and spacing against Voyage's rate limit."""
    path = os.environ.get("EMBED_TRACE")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(json.dumps({"ts": round(time.time(), 2), "pid": os.getpid(), "input_type": input_type, "ok": ok,
                                "ms": round((time.monotonic() - t0) * 1000), "err": err}) + "\n")
    except OSError:
        pass


def embed_with_model(text: str, input_type: str = "document") -> tuple[list[float], str]:
    """Embed ``text`` and say which model ACTUALLY produced the vector.

    If Voyage is configured but the call fails or times out, we fall back to the local model so the demo
    never dies or stalls on a network blip (and skip Voyage for EMBED_COOLDOWN_S so a dead network costs
    one wait, not one per call). The returned model id says which model was used, so a fallback vector is
    never stored or queried under Voyage's label (the two vector spaces must never mix)."""
    global _voyage_down_until
    if config.VOYAGE_API_KEY and time.monotonic() >= _voyage_down_until:
        t0 = time.monotonic()
        try:
            vec = _voyage(text, input_type)
            _trace(input_type, True, t0)
            return vec, config.VOYAGE_MODEL
        except Exception as e:
            _trace(input_type, False, t0, type(e).__name__)
            _voyage_down_until = time.monotonic() + config.EMBED_COOLDOWN_S
    return _local(text), LOCAL_MODEL


def embed(text: str, input_type: str = "document") -> list[float]:
    return embed_with_model(text, input_type)[0]


def model_id() -> str:
    """Stored on every playbook as `embedding_model`; recall filters on it so vector spaces never mix."""
    return config.VOYAGE_MODEL if config.VOYAGE_API_KEY else LOCAL_MODEL
