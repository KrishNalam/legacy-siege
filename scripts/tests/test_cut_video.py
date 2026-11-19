"""Video tooling checks that need no ffmpeg, Pillow, TTS engine or database."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import beatlog, code_cards, cut_video, narration

NAMES = ["night1_attack", "cascade_trace", "lie_caught", "approval", "kill_takeover", "night2_recall",
         "night2_resolved", "upgrader", "night3", "atlas"]
LOG = {"video_duration_s": 200.0, "metrics": {},
       "beats": [{"name": n, "t_start": 10.0 * i, "t_end": 10.0 * i + 20.0} for i, n in enumerate(NAMES)]}
CARDS = [{"key": k, "caption": k} for k in ("lease", "verifier", "schema")]
# the same take with the 10-beat script's moments recorded (record_demo's script beats)
NEW = ["ai_checked", "ai_overruled", "schema_drift", "drift_reused", "red_freeze"]
FULL = {**LOG, "video_duration_s": 400.0,
        "beats": LOG["beats"] + [{"name": n, "t_start": 200.0 + 20 * i, "t_end": 212.0 + 20 * i}
                                 for i, n in enumerate(NEW)]}
SCRIPT = Path(__file__).resolve().parents[2] / "docs" / "video-script.md"
TABLE_KEYS = {"Hook": "hook", "Cascade": "cascade", "Claude, checked": "ai", "The lie": "lie", "Kill + Nurse": "kill",
              "Code": "code", "Round 2": "learn", "Schema drift": "drift", "Red fleet": "red", "Close": "garden"}


def segments(pieces):
    """[(segment key, start, length)] in cut order."""
    out, t = [], 0.0
    for p in pieces:
        if out and out[-1][0] == p["seg"]:
            out[-1][2] += p["slot"]
        else:
            out.append([p["seg"], t, p["slot"]])
        t += p["slot"]
    return [(k, round(a, 2), round(n, 2)) for k, a, n in out]


@pytest.mark.parametrize("log", [LOG, FULL], ids=["old-take", "full-take"])
@pytest.mark.parametrize("layout", cut_video.LAYOUTS)
@pytest.mark.parametrize("cards", [CARDS, CARDS[:1], []])
def test_plan_totals_exactly_60_s(layout, cards, log):
    pieces = cut_video.resolve(log, 60.0, layout, cards)
    assert sum(p["slot"] for p in pieces) == pytest.approx(60.0, abs=0.01)
    code = [p for p in pieces if "card" in p]
    assert [p["card"] for p in code] == [c["key"] for c in cards]
    if cards:
        assert sum(p["slot"] for p in code) == pytest.approx(cut_video.code_s(layout), abs=0.01)
        assert pieces.index(code[0]) > [p["beat"] for p in pieces].index("kill_takeover")
    assert all(p["slot"] > 0 for p in pieces)


def test_old_layouts_unchanged():
    assert cut_video.CODE_S == 9.0
    keys = [k for k, _, _ in segments(cut_video.resolve(FULL, 60.0, "brief", CARDS))]
    assert keys == ["hook", "cascade", "lie", "kill", "code", "learn", "garden"]
    assert [s["key"] for s in cut_video.plan(LOG, "doc", CARDS)] == keys


def script_table():
    """[(segment key, start, length)] from the timing table in docs/video-script.md."""
    rows = re.findall(r"^\|\s*(\d+)–(\d+) s\s*\|\s*\*\*(.+?)\*\*", SCRIPT.read_text(), re.M)
    assert len(rows) == 10, rows
    return [(TABLE_KEYS[name], float(a), float(b) - float(a)) for a, b, name in rows]


@pytest.mark.parametrize("log", [FULL, LOG], ids=["full-take", "fallbacks"])
def test_script_layout_follows_the_script_table(log):
    """Default cut = the 10 beats at the script's times, whether the moments were recorded or fell back."""
    pieces = cut_video.resolve(log, 60.0, "script", CARDS)
    got, want = segments(pieces), script_table()
    assert [k for k, _, _ in got] == [k for k, _, _ in want]
    for (_, a, n), (_, a0, n0) in zip(got, want):
        assert (a, n) == (pytest.approx(a0, abs=0.05), pytest.approx(n0, abs=0.05))
    assert cut_video.plan(log, cards=CARDS)[2]["key"] == "ai"  # "script" is the default layout


def test_missing_script_beats_fall_back_to_windows_with_captions():
    pieces = cut_video.resolve(LOG, 60.0, "script", CARDS)
    by = {p["beat"]: p for p in pieces}
    beats = {b["name"]: b for b in LOG["beats"]}
    for name, (anchor, edge, d0, d1, caption) in cut_video.FALLBACK.items():
        ref = beats[anchor]["t_start" if edge == "start" else "t_end"]
        assert cut_video.window(beats, name, 200.0) == (ref + d0, ref + d1, caption)
        p = by[name]  # resolve() may still trim / grow the window against its neighbours
        assert p["fallback"] and p["caption"] == caption and p["a"] < ref + d1 and p["b"] > ref + d0
    assert "ai_overruled" not in by and "drift_reused" not in by  # their seconds stay in their segment
    assert {k: n for k, _, n in segments(pieces)}["ai"] == pytest.approx(9.0, abs=0.05)
    assert not any(p.get("fallback") for p in cut_video.resolve(FULL, 60.0, "script", CARDS))


def test_at_overrides_a_beat_window():
    at = cut_video.parse_at(["red_freeze=150.5-158"])
    assert at == {"red_freeze": (150.5, 158.0)}
    p = next(p for p in cut_video.resolve(LOG, 60.0, "script", CARDS, at) if p["beat"] == "red_freeze")
    assert (p["a"], p["b"], p["fallback"]) == (150.5, 158.0, False)
    with pytest.raises(SystemExit):
        cut_video.parse_at(["red_freeze=9-3"])


def test_captions_use_the_take():
    cap = cut_video.script_captions({"beats": [
        {"name": "ai_checked", "t_start": 1, "t_end": 2, "decision": "ai_plan"},
        {"name": "ai_overruled", "t_start": 1, "t_end": 2, "op": "restart_batch", "rule": "P3 blast radius"},
        {"name": "schema_drift", "t_start": 1, "t_end": 2, "from": "balance", "to": "acct_bal", "version": 2},
        {"name": "red_freeze", "t_start": 1, "t_end": 2, "by": "red-assassin", "agent": "operator-2", "takeover_s": 11.2}]})
    assert "CLAUDE'S PLAN RUNS" in cap["ai"] and "restart_batch → P3 blast radius" in cap["overruled"]
    assert "balance → acct_bal" in cap["drift"] and "MAPPING LEARNED" in cap["drift"]
    assert "red-assassin FREEZEs operator-2" in cap["red"] and "11 s" in cap["red"]


def test_narration_covers_every_segment():
    spoken = narration.paragraphs()
    for layout in cut_video.LAYOUTS:
        assert {s["key"] for s in cut_video.plan(LOG, layout, CARDS)} <= set(spoken)
    # every paragraph of the script has its segment in the default cut, so --tts speaks all of it
    assert {s["key"] for s in cut_video.plan(LOG, "script", CARDS)} == set(spoken)
    assert 120 <= sum(len(t.split()) for t in spoken.values()) <= 160
    plain = (SCRIPT.parent / "video-narration.txt").read_text().split()
    assert plain == " ".join(spoken[k] for k, _, _ in script_table()).split()


def test_narration_fits_its_segment():
    """At a brisk 3 words/s (the script's average is ~2.5), every paragraph fits its segment within
    narration_inputs' ×1.25 warning; a longer paragraph needs trimming in docs/video-script.md."""
    spoken = narration.paragraphs()
    for key, _, secs in segments(cut_video.resolve(FULL, 60.0, "script", CARDS)):
        room = max(secs - cut_video.VO_LEAD - 0.15, 1.0)
        assert len(spoken[key].split()) / 3.0 / room <= 1.25, key


def test_narration_inputs_start_each_clip_at_its_segment():
    pieces = cut_video.resolve(FULL, 60.0, "script", CARDS)
    t = 0.0
    for p in pieces:
        p["at"], t = round(t, 3), t + p["slot"]
    clips = {k: (Path(f"/tmp/{k}.wav"), 2.0) for k in ("ai", "red", "nope")}
    ins, graph = cut_video.narration_inputs(pieces, clips, 60.0, first=1)
    assert ins == ["-i", "/tmp/ai.wav", "-i", "/tmp/red.wav"]
    starts = {k: a for k, a, _ in segments(pieces)}
    assert f"adelay={int((starts['ai'] + cut_video.VO_LEAD) * 1000)}" in graph
    assert f"adelay={int((starts['red'] + cut_video.VO_LEAD) * 1000)}" in graph


def ev(name, t, **kw):
    return {"name": name, "t": t, **kw}


def test_script_beats_from_recorded_events():
    events = [ev("ai_thinking", 20.0, incident="INC-1"),
              ev("ai_decided", 24.0, incident="INC-1", decision="rules_plan"),
              ev("ai_overruled", 24.0, incident="INC-1", op="restart_batch", rule="P3 blast radius"),
              ev("ai_thinking", 90.0, incident="INC-2"),
              ev("ai_decided", 93.0, incident="INC-2", decision="ai_plan"),
              ev("drift_noticed", 300.0), ev("mapping_learned", 312.0, frm="balance", to="acct_bal", version=2),
              ev("mapping_reused", 400.0, version=2),
              ev("takeover", 50.0, incident="INC-1", to="recovery-1"),  # the SIGKILL takeover, before the freeze
              ev("red_freeze", 200.0, id="CHX-9", by="red-assassin"),
              ev("takeover", 211.0, incident="INC-3", to="recovery-2"),
              ev("red_thaw", 225.0, id="CHX-9", by="red-assassin", agent="operator-2", ok=True)]
    beats = {b["name"]: b for b in beatlog.script_beats(events, lambda e, d=0.0: round(max(0.0, e["t"] - 1.0 + d), 2))}
    assert beats["ai_checked"]["incident"] == "INC-2" and beats["ai_checked"]["decision"] == "ai_plan"
    assert (beats["ai_checked"]["t_start"], beats["ai_checked"]["t_end"]) == (88.0, 95.5)
    assert beats["ai_overruled"]["rule"] == "P3 blast radius"
    assert beats["schema_drift"]["t_start"] == 298.0 and beats["schema_drift"]["to"] == "acct_bal"
    assert beats["drift_reused"]["version"] == 2
    red = beats["red_freeze"]
    assert (red["t_start"], red["t_end"], red["takeover_s"], red["agent"]) == (196.0, 213.0, 11.0, "operator-2")
    assert beatlog.script_beats([], lambda e, d=0.0: e["t"]) == []


def test_code_cards_use_real_code():
    keys = {c["key"] for c in code_cards.usable()}
    assert {"lease", "verifier"} <= keys  # schema needs agents/schema.py (tree or the drift branch)
    src, _ = code_cards.source(next(c for c in code_cards.CARDS if c["key"] == "lease"))
    rows = code_cards.excerpt(src, next(c for c in code_cards.CARDS if c["key"] == "lease")["ranges"])
    assert any("find_one_and_update" in t for _, t in rows)
    assert all(len(w) <= code_cards.MAXCOL for _, t in rows for w in code_cards.wrap(t))
