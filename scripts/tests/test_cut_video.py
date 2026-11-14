"""Video tooling checks that need no ffmpeg, Pillow, TTS engine or database."""
from __future__ import annotations

import pytest

from scripts import code_cards, cut_video, narration

NAMES = ["night1_attack", "cascade_trace", "lie_caught", "approval", "kill_takeover", "night2_recall",
         "night2_resolved", "gardener", "night3", "atlas"]
LOG = {"video_duration_s": 200.0, "metrics": {},
       "beats": [{"name": n, "t_start": 10.0 * i, "t_end": 10.0 * i + 20.0} for i, n in enumerate(NAMES)]}
CARDS = [{"key": k, "caption": k} for k in ("lease", "guard", "schema")]


@pytest.mark.parametrize("layout", ["brief", "doc"])
@pytest.mark.parametrize("cards", [CARDS, CARDS[:1], []])
def test_plan_totals_exactly_60_s(layout, cards):
    pieces = cut_video.resolve(LOG, 60.0, layout, cards)
    assert sum(p["slot"] for p in pieces) == pytest.approx(60.0, abs=0.01)
    code = [p for p in pieces if "card" in p]
    assert [p["card"] for p in code] == [c["key"] for c in cards]
    if cards:
        assert sum(p["slot"] for p in code) == pytest.approx(cut_video.CODE_S, abs=0.01)
        assert pieces.index(code[0]) > [p["beat"] for p in pieces].index("kill_takeover")


def test_narration_covers_every_segment():
    spoken = narration.paragraphs()
    segs = {s["key"] for s in cut_video.plan(LOG, "brief", CARDS)}
    assert segs <= set(spoken)
    assert 120 <= sum(len(t.split()) for t in spoken.values()) <= 160


def test_code_cards_use_real_code():
    keys = {c["key"] for c in code_cards.usable()}
    assert {"lease", "guard"} <= keys  # schema needs swarm/schema.py (tree or the drift branch)
    src, _ = code_cards.source(next(c for c in code_cards.CARDS if c["key"] == "lease"))
    rows = code_cards.excerpt(src, next(c for c in code_cards.CARDS if c["key"] == "lease")["ranges"])
    assert any("find_one_and_update" in t for _, t in rows)
    assert all(len(w) <= code_cards.MAXCOL for _, t in rows for w in code_cards.wrap(t))
