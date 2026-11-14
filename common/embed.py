"""Embeddings for playbook memory ("honey").

Voyage AI when VOYAGE_API_KEY is set; otherwise a deterministic local hashing embedding
(token + bigram feature hashing, L2-normalised) with the same dimension so the Atlas
Vector Search index works either way. Switching providers requires re-embedding playbooks
(`python -m scripts.setup_db --reset` wipes them).
"""
from __future__ import annotations

import hashlib
import math
import re

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


def embed_with_model(text: str, input_type: str = "document") -> tuple[list[float], str]:
    """Embed ``text`` and say which model ACTUALLY produced the vector.

    If Voyage is configured but the call fails, we fall back to the local model so the demo never
    dies on a network blip — and the returned model id says so, so a fallback vector is never stored
    or queried under Voyage's label (the two vector spaces must never mix)."""
    if config.VOYAGE_API_KEY:
        try:
            import voyageai

            vo = voyageai.Client(api_key=config.VOYAGE_API_KEY)
            r = vo.embed([text], model=config.VOYAGE_MODEL, input_type=input_type, output_dimension=config.EMBED_DIM)
            return list(r.embeddings[0]), config.VOYAGE_MODEL
        except Exception:
            pass
    return _local(text), LOCAL_MODEL


def embed(text: str, input_type: str = "document") -> list[float]:
    return embed_with_model(text, input_type)[0]


def model_id() -> str:
    """Stored on every playbook as `embedding_model`; recall filters on it so vector spaces never mix."""
    return config.VOYAGE_MODEL if config.VOYAGE_API_KEY else LOCAL_MODEL
