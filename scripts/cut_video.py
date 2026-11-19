"""Cut the 60-second submission video from a raw war-room recording and its beat log (scripts.record_demo).

    python -m scripts.cut_video ~/projects/legacy-siege-media/beats-<ts>.json
    python -m scripts.cut_video beats.json --out legacy-siege-60s.mp4 --voiceover vo.mp3
    python -m scripts.cut_video beats.json --print-plan          # show the source windows / speeds, cut nothing
    python -m scripts.cut_video beats.json --layout brief        # the older 7-segment cut (also: doc)
    python -m scripts.cut_video beats.json --at red_freeze=212-220   # point a beat at a raw-video window
    python -m scripts.cut_video beats.json --tts                 # offline narration from docs/video-script.md
    python -m scripts.cut_video beats.json --no-code             # skip the CODE segment

Needs a full ffmpeg on PATH (libx264, drawtext, aac). Each beat's source window is sped up (setpts) to fit
its slot, so waiting stretches go fast but nothing is dropped. Captions name the mechanism on screen.

Layouts: "script" (default) follows docs/video-script.md, 10 segments
hook/cascade/ai/lie/kill/code/learn/drift/red/garden = 5/5/9/5/6/8/6/5/6/5 s; "brief" and "doc" are the
older 7-segment cuts. A beat the recording has no event for (Claude's card needs AI_DRIVES, the drift beat
`record_demo --drift`, the FREEZE beat RED_FLEET=1) falls back to a fixed window around another beat with a
plainer caption (marked "fallback" in the plan); `--at BEAT=START-END` points it at real footage instead.

CODE segment (default on, `--no-code` to drop): 8 s (9 s in the old layouts) of syntax-highlighted code cards (scripts/code_cards.py,
Pillow is fetched with `uv run --no-project --with pillow` if the venv has none) between the kill and the
learning beats, because the rules want the video to show the code as well as the functionality. The other
beats give up their seconds, so the total stays exactly --length.

Audio: --voiceover FILE mixes a human recording (trimmed / padded to the video length); --tts speaks
docs/video-script.md with an offline engine (scripts/narration.py), each paragraph starting with its
segment; with neither, a silent track.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:  # `python -m scripts.cut_video` and `python scripts/cut_video.py` both work
    from scripts import code_cards, narration
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import code_cards  # type: ignore[no-redef]
    import narration  # type: ignore[no-redef]

FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
GREEN = "0x00ED64"
FPS = 30
KEEP_TAIL = 1.5   # a beat's key moment sits at least this long before its t_end (see record_demo.derive_beats)
MIN_SPEED = 0.85  # slowest playback allowed; below it a beat's slot shrinks instead
CODE_S = 9.0      # length of the CODE segment in the old layouts (brief, doc)
SCRIPT_CODE_S = 8.0  # ... and in the default "script" layout (docs/video-script.md, 30–38 s)
LAYOUTS = ("script", "brief", "doc")
# beat -> fallback when the log has no such beat: (anchor beat, "start"|"end", from, to (s around the edge), caption)
FALLBACK = {
    "ai_checked": ("cascade_trace", "end", -3.0, 6.0,
                   "Claude diagnoses and proposes the fix; the P1–P6 safety checks decide whether it runs"),
    "schema_drift": ("night2_resolved", "end", 0.0, 6.0,  # the green estate after round 2 (unused in "script")
                     "Schema drift: a renamed feed field is diffed in MongoDB, mapped, verified, remembered"),
    "red_freeze": ("kill_takeover", "end", -8.0, -1.0,
                   "Red strikers observe → think → strike → reflect; a frozen agent's lease expires, a Nurse takes over"),
}


def code_s(layout: str) -> float:
    return SCRIPT_CODE_S if layout == "script" else CODE_S
VO_LEAD = 0.3     # narration starts this long after its segment does


def fmt_s(v) -> str:
    try:
        return f"{float(v):.0f} s" if float(v) >= 9.5 else f"{float(v):.1f} s"
    except (TypeError, ValueError):
        return "?"


def _beat(log: dict, name: str) -> dict:
    return next((b for b in log.get("beats") or [] if b["name"] == name), {})


def script_captions(log: dict) -> dict[str, str]:
    """Captions for the "script" layout, with the take's own numbers where the beat log carries them."""
    ai, ov = _beat(log, "ai_checked"), _beat(log, "ai_overruled")
    dr, ru, red = _beat(log, "schema_drift"), _beat(log, "drift_reused"), _beat(log, "red_freeze")
    rule = f"{ov.get('op') or 'unsafe op'} → {ov.get('rule') or 'P3 blast radius'}"
    field = f"{dr.get('from') or 'balance'} → {dr.get('to') or 'acct_bal'}"
    who, victim = red.get("by") or "Red striker", red.get("agent") or "a worker"
    took = f" in {fmt_s(red['takeover_s'])}" if red.get("takeover_s") is not None else ""
    return {
        "hook2": "12 AI agents defend a legacy bank. AI attackers try to break it.",
        "ai": ("Claude's plan passed P1–P6 → CLAUDE'S PLAN RUNS" if ai.get("decision") == "ai_plan"
               else "Claude ranks hypotheses + proposes a plan; P1–P6 safety checks judge every op"),
        "overruled": f"Unsafe idea overruled: {rule}",
        "drift": f"unix feed schema changed: {field} → MAPPING LEARNED"
                 + (f" (v{dr['version']})" if dr.get("version") else ""),
        "reused": "Same change again → MAPPING REUSED from MongoDB, no Claude call",
        "red": f"{who} FREEZEs {victim} → its lease expires → a Nurse takes over{took}",
    }


def plan(log: dict, layout: str = "script", cards: list[dict] | None = None) -> list[dict]:
    """The edit: segments of (slot seconds, kicker, [pieces]); each piece = (beat name, seconds, caption).

    `cards` = the usable code cards (code_cards.usable()); with any, a CODE segment of code_s(layout) seconds
    is inserted after the kill and the other beats shrink so the total stays 60 s (else the old timings):
      layout "script" (default): docs/video-script.md, hook/cascade/ai/lie/kill/code/learn/drift/red/garden =
                  5/5/9/5/6/8/6/5/6/5 s (without code 6/6/10/6/7/-/7/6/7/5).
      layout "brief": hook/cascade/lie/kill/code/learn/garden = 7/8/9/9/9/9/9 s (was 7/10/9/10/-/12/12),
                  ends on night 3 + the Atlas view (the cut before the 10-beat script).
      layout "doc": the design doc's cut, 5/12/8/11/9/9/6 s (was 5/15/10/15/-/10/5).
    A "card:<key>" piece is a code card, not footage.
    """
    code = bool(cards)
    m = log.get("metrics") or {}
    n1, n2 = m.get("night1") or {}, m.get("night2") or {}
    learn = (f"Round 2: $vectorSearch recall → diagnosis {fmt_s(n1.get('diagnosis_s'))} → "
             f"{fmt_s(n2.get('diagnosis_s'))}, {n1.get('hops', 3)} hops → {n2.get('hops', 1)}")
    cap = {
        "hook": "Chaos Monkey hits a legacy bank. 12 AI agents have to keep it alive.",
        "cascade": "Scout walks the blame chain portal → unix → mainframe (portal still says 200)",
        "lie": "Op says applied:true. Guard reads it back: ACK ≠ APPLIED → new attempt",
        "approval": "Destructive step waits for the Beekeeper's approval before it is sent",
        "kill": "Real SIGKILL → Nurse resumes from the MongoDB checkpoint",
        "gardener": "Gardener turns the repeat fault into a code upgrade: v1 → v2, tested + approved",
        "night3": "Night 3: the same attack. Nothing happens.",
        "atlas": "MongoDB is the hive's memory: state, leases, checkpoints, playbooks",
    }

    def t(with_code: float, without: float) -> float:
        return with_code if code else without

    cs = code_s(layout)
    seg_code = ([{"key": "code", "kicker": "UNDER THE HOOD · THE REAL CODE",
                  "pieces": [(f"card:{c['key']}", round(cs / len(cards), 3), c["caption"]) for c in cards]}]
                if code else [])
    hook = {"key": "hook", "kicker": "LEGACY SIEGE · MONGODB IS THE SPINE", "title": True}
    if layout == "script":
        cap.update(script_captions(log))
        return [
            {**hook, "pieces": [("night1_attack", t(5.0, 6.0), cap["hook2"])]},
            {"key": "cascade", "kicker": "1 · CASCADE", "pieces": [("cascade_trace", t(5.0, 6.0), cap["cascade"])]},
            {"key": "ai", "kicker": "2 · CLAUDE, CHECKED", "pieces": [
                ("ai_checked", t(5.5, 6.0), cap["ai"]), ("ai_overruled", t(3.5, 4.0), cap["overruled"])]},
            {"key": "lie", "kicker": "3 · THE LYING SYSTEM", "pieces": [("lie_caught", t(5.0, 6.0), cap["lie"])]},
            {"key": "kill", "kicker": "4 · REAL SIGKILL", "pieces": [("kill_takeover", t(6.0, 7.0), cap["kill"])]},
            *seg_code,
            {"key": "learn", "kicker": "5 · THE HIVE LEARNS", "pieces": [
                ("night2_recall", t(4.5, 5.0), learn), ("night2_resolved", t(1.5, 2.0), learn)]},
            {"key": "drift", "kicker": "6 · SCHEMA DRIFT", "pieces": [
                ("schema_drift", t(3.0, 3.5), cap["drift"]), ("drift_reused", t(2.0, 2.5), cap["reused"])]},
            {"key": "red", "kicker": "7 · RED FLEET", "pieces": [("red_freeze", t(6.0, 7.0), cap["red"])]},
            {"key": "garden", "kicker": "MONGODB IS THE HIVE'S MEMORY", "pieces": [
                ("night3", 2.0, "Every frame is a MongoDB change stream"), ("atlas", 3.0, cap["atlas"])]},
        ]
    if layout == "doc":
        return [
            {**hook, "pieces": [("night1_attack", 5.0, cap["hook"])]},
            {"key": "cascade", "kicker": "1 · CASCADE", "pieces": [("cascade_trace", t(12.0, 15.0), cap["cascade"])]},
            {"key": "lie", "kicker": "2 · THE LYING SYSTEM", "pieces": [("lie_caught", t(8.0, 10.0), cap["lie"])]},
            {"key": "kill", "kicker": "3 · REAL SIGKILL", "pieces": [
                ("approval", t(3.0, 4.0), cap["approval"]), ("kill_takeover", t(8.0, 11.0), cap["kill"])]},
            *seg_code,
            {"key": "learn", "kicker": "4 · THE HIVE LEARNS", "pieces": [
                ("night2_recall", t(7.0, 7.0), learn), ("night2_resolved", t(2.0, 3.0), learn)]},
            {"key": "garden", "kicker": "5 · FIX IT FOR GOOD", "pieces": [
                ("gardener", t(4.0, 3.0), cap["gardener"]), ("night3", 2.0, cap["night3"])]},
        ]
    return [
        {**hook, "pieces": [("night1_attack", 7.0, cap["hook"])]},
        {"key": "cascade", "kicker": "1 · CASCADE", "pieces": [("cascade_trace", t(8.0, 10.0), cap["cascade"])]},
        {"key": "lie", "kicker": "2 · THE LYING SYSTEM", "pieces": [("lie_caught", 9.0, cap["lie"])]},
        {"key": "kill", "kicker": "3 · REAL SIGKILL", "pieces": [
            ("approval", t(2.5, 3.0), cap["approval"]), ("kill_takeover", t(6.5, 7.0), cap["kill"])]},
        *seg_code,
        # {"key": "drift", ...}  schema drift beat goes here after the code freeze (steal ~5 s from "learn")
        {"key": "learn", "kicker": "4 · THE HIVE LEARNS", "pieces": [
            ("night2_recall", t(6.5, 9.0), learn), ("night2_resolved", t(2.5, 3.0), learn)]},
        {"key": "garden", "kicker": "5 · FIX IT FOR GOOD", "pieces": [
            ("gardener", t(4.0, 5.0), cap["gardener"]), ("night3", t(2.5, 3.0), cap["night3"]),
            ("atlas", t(2.5, 4.0), cap["atlas"])]},
    ]


def window(beats: dict, name: str, dur: float) -> tuple[float, float, str] | None:
    """Fallback source window for a beat the log lacks: (a, b, caption) around its anchor beat, else None."""
    fb = FALLBACK.get(name)
    anchor = fb and beats.get(fb[0])
    if not anchor:
        return None
    edge = anchor["t_start"] if fb[1] == "start" else anchor["t_end"]
    a, b = max(0.0, edge + fb[2]), min(dur, edge + fb[3])
    return (round(a, 2), round(b, 2), fb[4]) if b - a >= 1.0 else None


def resolve(log: dict, total: float, layout: str = "script", cards: list[dict] | None = None,
            at: dict[str, tuple[float, float]] | None = None) -> list[dict]:
    """Map pieces to source windows, scaled to `total`. Code cards keep their seconds; only the footage pieces
    are scaled and retimed. `at` = {beat: (start, end)} overrides the log. A missing beat uses its FALLBACK
    window (caption marked as such); without one its seconds go to the segment's other pieces, and a segment
    with no footage at all gives its seconds to the rest of the cut."""
    beats = {b["name"]: b for b in log["beats"]}
    for name, (a, b) in (at or {}).items():
        beats[name] = {**beats.get(name, {}), "name": name, "t_start": a, "t_end": b, "override": True}
    dur = float(log.get("video_duration_s") or 1e9)
    pieces = []
    for seg in plan(log, layout, cards):
        spare, mine = 0.0, []
        for i, (name, secs, cap) in enumerate(seg["pieces"]):
            if name.startswith("card:"):
                pieces.append({"seg": seg["key"], "kicker": seg["kicker"], "title": False, "beat": name,
                               "card": name[5:], "slot": secs, "caption": cap, "speed": 1.0})
                continue
            b = beats.get(name)
            fallback = False
            if b:
                a0, b0 = b["t_start"], b["t_end"]
            elif (w := window(beats, name, dur)):
                a0, b0, cap = w
                fallback = True
                print(f"  ! beat '{name}' missing from the log, fallback window {a0:.1f}-{b0:.1f} s", file=sys.stderr)
            else:
                print(f"  ! beat '{name}' missing from the log, skipped", file=sys.stderr)
                spare += secs
                continue
            mine.append({"seg": seg["key"], "kicker": seg["kicker"], "title": bool(seg.get("title") and i == 0),
                         "beat": name, "slot": secs, "caption": cap, "a": a0, "b": b0, "fallback": fallback})
        if mine and spare:  # keep the segment (and its narration) on the script's timeline
            for p in mine:
                p["slot"] += spare * p["slot"] / sum(q["slot"] for q in mine)
        pieces += mine
    everything, pieces = pieces, [p for p in pieces if "card" not in p]
    scale = (total - sum(p["slot"] for p in everything if "card" in p)) / sum(p["slot"] for p in pieces)
    for p in pieces:
        p["slot"] *= scale
        p["keep"] = p["b"] - KEEP_TAIL  # beats end a few seconds after their key moment; never cut before it
    # 1) no source second is shown twice: where two windows overlap, the later piece starts the cut
    for prev, cur in zip(pieces, pieces[1:]):
        if prev["b"] > cur["a"] >= prev["a"]:
            cut = max(cur["a"], min(prev["keep"], cur["b"] - 1.0))
            prev["b"], cur["a"] = cut, cut
    # 2) a window shorter than its slot grows into free footage: forward up to the next piece, then backward
    for i, p in enumerate(pieces):
        if p["b"] - p["a"] < p["slot"]:
            nxt = pieces[i + 1]["a"] if i + 1 < len(pieces) and pieces[i + 1]["a"] >= p["a"] else dur
            p["b"] = max(p["b"], min(dur, nxt, p["a"] + p["slot"]))
        if p["b"] - p["a"] < p["slot"] and i:
            prev = pieces[i - 1]
            floor = max(prev["keep"], prev["a"] + 1.0) if prev["b"] <= p["a"] + 0.01 else 0.0
            new_a = max(p["b"] - p["slot"], floor, 0.0)
            if new_a < p["a"]:
                p["a"] = new_a
                prev["b"] = min(prev["b"], new_a)
    # 3) still too short: shrink the slot (never slower than MIN_SPEED) and hand the seconds to the next
    #    piece with footage to spare, so later segments keep their place on the timeline
    for i, p in enumerate(pieces):
        need = (p["b"] - p["a"]) / MIN_SPEED
        if need < p["slot"] - 0.05:
            spare = p["slot"] - need
            p["slot"] = need
            for q in pieces[i + 1:] + pieces[:i][::-1]:
                room = (q["b"] - q["a"]) - q["slot"]  # seconds it can absorb at ≥1× speed
                give = min(spare, max(room, 0.0))
                q["slot"] += give
                spare -= give
                if spare <= 0.01:
                    break
    for p in pieces:
        p["slot"] = round(p["slot"], 3)
        p["speed"] = round((p["b"] - p["a"]) / p["slot"], 3)
    drift = total - sum(p["slot"] for p in everything)
    last = everything[-1]
    last["slot"] = round(last["slot"] + drift, 3)
    if "card" not in last:
        last["speed"] = round((last["b"] - last["a"]) / last["slot"], 3)
    return everything


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.stderr.write(r.stderr[-3000:])
        raise SystemExit(f"ffmpeg failed: {' '.join(cmd[:6])} …")


def render_cards(out_dir: Path) -> dict[str, str]:
    """Render the code cards -> {key: png path}. Pillow in this interpreter if there is one, else a throwaway
    `uv run --with pillow` (the repo venv doesn't carry it)."""
    import importlib.util  # noqa: PLC0415
    if importlib.util.find_spec("PIL"):
        res = code_cards.build(out_dir)
    else:
        if not shutil.which("uv"):
            raise SystemExit("code cards need Pillow: `pip install pillow` or install uv (or pass --no-code)")
        r = subprocess.run(["uv", "run", "--no-project", "--with", "pillow", "python", str(Path(code_cards.__file__)),
                            "--out", str(out_dir), "--json"], capture_output=True, text=True)
        sys.stderr.write(r.stderr)
        if r.returncode:
            raise SystemExit("rendering the code cards failed (see above)")
        res = json.loads(r.stdout.strip().splitlines()[-1])
    return {c["key"]: c["png"] for c in res}


def narration_inputs(pieces: list[dict], clips: dict, length: float, first: int) -> tuple[list[str], str]:
    """ffmpeg inputs + filter graph (output [aout]) placing each segment's TTS clip at its segment start.
    Clips are sped up (atempo, never slowed) just enough to fit their segment; `first` = index of the first input."""
    segs: dict[str, list[float]] = {}
    for p in pieces:
        s = segs.setdefault(p["seg"], [p["at"], 0.0])
        s[1] += p["slot"]
    inputs, chains, labels = [], [], []
    for key, (wav, dur) in clips.items():
        if key not in segs:
            continue
        start, slot = segs[key]
        room = max(slot - VO_LEAD - 0.15, 1.0)
        tempo = max(1.0, dur / room)
        if tempo > 1.25:
            print(f"  ! narration '{key}' is {dur:.1f} s for a {slot:.1f} s segment: speeding it up ×{tempo:.2f}"
                  " (shorten its paragraph in docs/video-script.md)", file=sys.stderr)
        i = first + len(labels)
        inputs += ["-i", str(wav)]
        ms = int((start + VO_LEAD) * 1000)
        chains.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,atempo={min(tempo, 2.0):.4f},"
                      f"adelay={ms}|{ms}[v{i}]")
        labels.append(f"[v{i}]")
    L = f"{length:.3f}"
    mix = (f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,loudnorm=I=-16:TP=-1.5:LRA=11,"
           f"aresample=48000,apad,atrim=0:{L},afade=t=out:st={length - 0.8:.3f}:d=0.8[aout]")
    return inputs, ";".join(chains + [mix])


def esc(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def parse_at(specs: list[str]) -> dict[str, tuple[float, float]]:
    """--at BEAT=START-END (seconds into the raw video) -> {beat: (start, end)}."""
    out = {}
    for spec in specs or []:
        try:
            name, rng = spec.split("=", 1)
            a, b = (float(x) for x in rng.split("-", 1))
        except ValueError:
            raise SystemExit(f"--at {spec!r}: expected BEAT=START-END, e.g. red_freeze=212.5-220") from None
        if b <= a:
            raise SystemExit(f"--at {spec!r}: END must be after START")
        out[name.strip()] = (a, b)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("beats", type=Path, help="beats-<ts>.json written by scripts.record_demo")
    ap.add_argument("--video", type=Path, help="raw video (default: the one named in the beat log)")
    ap.add_argument("--out", type=Path, help="default: legacy-siege-60s.mp4 next to the beat log")
    ap.add_argument("--voiceover", type=Path, help="audio file to mix in (else a silent track)")
    ap.add_argument("--tts", action="store_true",
                    help="no recording? narrate docs/video-script.md with an offline TTS engine (ignored with --voiceover)")
    ap.add_argument("--tts-engine", choices=narration.ENGINES, help="default: piper, else espeak, else flite")
    ap.add_argument("--code", action=argparse.BooleanOptionalAction, default=True,
                    help="8 s (old layouts 9 s) of code cards (agents/leases.py, verify.py, schema.py) after the kill")
    ap.add_argument("--vo-gain", type=float, default=1.0)
    ap.add_argument("--length", type=float, default=60.0)
    ap.add_argument("--layout", choices=LAYOUTS, default="script",
                    help="script: docs/video-script.md, 10 segments (default); brief: the older 7/8/9/9/9/9/9 s cut, "
                         "ends on Atlas; doc: the design doc's cut")
    ap.add_argument("--at", action="append", metavar="BEAT=START-END",
                    help="use this raw-video window for a beat (repeatable), e.g. a FREEZE the monitor missed")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--print-plan", action="store_true")
    a = ap.parse_args()
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not on PATH")
    log = json.loads(a.beats.read_text())
    video = a.video or a.beats.parent / log["video"]
    out = a.out or a.beats.parent / "legacy-siege-60s.mp4"
    cards = code_cards.usable() if a.code else []
    if a.code and not cards:
        print("  ! no code card source found, cutting without the code segment", file=sys.stderr)
    pieces = resolve(log, a.length, a.layout, cards, parse_at(a.at))
    t = 0.0
    for p in pieces:
        p["at"] = round(t, 3)
        t += p["slot"]
        src = "code card".ljust(24) if "card" in p else f"src {p['a']:7.1f}-{p['b']:7.1f}  ×{p['speed']:<5}"
        mark = "  (fallback)" if p.get("fallback") else ""
        print(f"  {p['at']:5.1f}-{t:5.1f}s  {p['beat']:<16} {src}  {p['caption']}{mark}")
    if a.print_plan:
        return 0
    with tempfile.TemporaryDirectory(prefix="cut-", dir=out.parent) as td:
        tmp = Path(td)
        # 1) each piece: seek, speed up to fit its slot, constant 30 fps, near-lossless intermediate
        pngs = render_cards(tmp / "cards") if cards else {}
        for i, p in enumerate(pieces):
            p["file"] = tmp / f"p{i:02d}.mp4"
            if "card" in p:  # a still code card held for its slot
                run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", pngs[p["card"]],
                     "-t", f"{p['slot']:.3f}", "-vf", f"fps={FPS},scale=1920:1080,setsar=1", "-an", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p", str(p["file"])])
                continue
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{p['a']:.3f}", "-t", f"{p['b'] - p['a']:.3f}", "-i", str(video),
                 "-vf", f"setpts=(PTS-STARTPTS)/{p['speed']},fps={FPS},scale=1920:1080:flags=lanczos,setsar=1",
                 "-t", f"{p['slot']:.3f}", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "12",
                 "-pix_fmt", "yuv420p", str(p["file"])])
        (tmp / "list.txt").write_text("".join(f"file '{p['file']}'\n" for p in pieces))
        # 2) captions: a lower-third band, kicker (green) + caption (white); title card over the hook
        vf = []
        for i, p in enumerate(pieces):
            (tmp / f"k{i}.txt").write_text(p["kicker"])
            (tmp / f"c{i}.txt").write_text(p["caption"])
        segs: dict[str, list[float]] = {}
        for p in pieces:
            s = segs.setdefault(p["kicker"], [p["at"], p["at"] + p["slot"]])
            s[1] = p["at"] + p["slot"]
        vf.append("drawbox=x=0:y=ih-168:w=iw:h=168:color=black@0.86:t=fill")
        vf.append(f"drawbox=x=0:y=ih-168:w=iw:h=4:color={GREEN}@0.9:t=fill")
        for i, p in enumerate(pieces):
            a0, a1 = p["at"], p["at"] + p["slot"]
            k0, k1 = segs[p["kicker"]]
            if p["at"] == k0:  # one kicker per segment
                vf.append(f"drawtext=fontfile={FONT}:textfile='{esc(tmp / f'k{i}.txt')}':fontcolor={GREEN}:fontsize=30:"
                          f"x=60:y=h-140:enable='between(t,{k0:.3f},{k1:.3f})'")
            vf.append(f"drawtext=fontfile={FONT}:textfile='{esc(tmp / f'c{i}.txt')}':fontcolor=white:fontsize=46:"
                      f"x=60:y=h-92:enable='between(t,{a0:.3f},{a1 - 0.001:.3f})'")
            if p["title"]:
                tt = min(3.2, p["slot"])
                (tmp / "title.txt").write_text("LEGACY SIEGE")
                (tmp / "sub.txt").write_text("A self-healing AI ops team. Every bit of its memory lives in MongoDB.")
                vf.append(f"drawbox=x=0:y=0:w=iw:h=ih-168:color=black@0.55:t=fill:enable='lt(t,{tt})'")
                vf.append(f"drawtext=fontfile={FONT}:textfile='{esc(tmp / 'title.txt')}':fontcolor=white:fontsize=150:"
                          f"x=(w-text_w)/2:y=(h-168)/2-120:enable='lt(t,{tt})'")
                vf.append(f"drawtext=fontfile={FONT_REG}:textfile='{esc(tmp / 'sub.txt')}':fontcolor={GREEN}:fontsize=44:"
                          f"x=(w-text_w)/2:y=(h-168)/2+60:enable='lt(t,{tt})'")
        L = f"{a.length:.3f}"
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt")]
        if a.tts and not a.voiceover:
            engine, clips = narration.narrate(tmp / "vo", a.tts_engine)
            print(f"narration: {engine}, {sum(d for _, d in clips.values()):.0f} s of speech")
            ins, af = narration_inputs(pieces, clips, a.length, first=1)
            cmd += ins
        elif a.voiceover:
            if a.tts:
                print("  ! --voiceover given, ignoring --tts", file=sys.stderr)
            cmd += ["-i", str(a.voiceover)]
            af = (f"[1:a]aresample=48000,aformat=channel_layouts=stereo,volume={a.vo_gain},apad,atrim=0:{L},"
                  f"afade=t=out:st={a.length - 0.8:.3f}:d=0.8[aout]")
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            af = f"[1:a]atrim=0:{L}[aout]"
        cmd += ["-filter_complex", f"[0:v]{','.join(vf)}[vout];{af}", "-map", "[vout]", "-map", "[aout]",
                "-t", L, "-c:v", "libx264", "-preset", "medium", "-crf", str(a.crf), "-pix_fmt", "yuv420p",
                "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
        run(cmd)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
