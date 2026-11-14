"""Cut the 60-second submission video from a raw war-room recording and its beat log (scripts.record_demo).

    python -m scripts.cut_video ~/projects/legacy-siege-media/beats-<ts>.json
    python -m scripts.cut_video beats.json --out legacy-siege-60s.mp4 --voiceover vo.mp3
    python -m scripts.cut_video beats.json --print-plan          # show the source windows / speeds, cut nothing
    python -m scripts.cut_video beats.json --layout doc          # the design doc's 5/15/10/15/10/5 s cut

Needs a full ffmpeg on PATH (libx264, drawtext, aac). Each beat's source window is sped up (setpts) to fit
its slot, so waiting stretches go fast but nothing is dropped. Captions name the mechanism on screen. The
output always has an audio track: silence, or --voiceover mixed in (trimmed / padded to the video length).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
GREEN = "0x00ED64"
FPS = 30
KEEP_TAIL = 1.5   # a beat's key moment sits at least this long before its t_end (see record_demo.derive_beats)
MIN_SPEED = 0.85  # slowest playback allowed; below it a beat's slot shrinks instead


def fmt_s(v) -> str:
    try:
        return f"{float(v):.0f} s" if float(v) >= 9.5 else f"{float(v):.1f} s"
    except (TypeError, ValueError):
        return "?"


def plan(log: dict, layout: str = "brief") -> list[dict]:
    """The edit: segments of (slot seconds, kicker, [pieces]); each piece = (beat name, seconds, caption).

    layout "brief" (default): 7/10/9/10/12/12 s, ends on night 3 + the Atlas view (matches docs/video-script.md).
    layout "doc": the design doc's cut, 5/15/10/15/10/5 s (hook, cascade, lie, kill, round 2, Gardener).
    """
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
    if layout == "doc":
        return [
            {"key": "hook", "kicker": "LEGACY SIEGE · MONGODB IS THE SPINE", "title": True,
             "pieces": [("night1_attack", 5.0, cap["hook"])]},
            {"key": "cascade", "kicker": "1 · CASCADE", "pieces": [("cascade_trace", 15.0, cap["cascade"])]},
            {"key": "lie", "kicker": "2 · THE LYING SYSTEM", "pieces": [("lie_caught", 10.0, cap["lie"])]},
            {"key": "kill", "kicker": "3 · REAL SIGKILL", "pieces": [
                ("approval", 4.0, cap["approval"]), ("kill_takeover", 11.0, cap["kill"])]},
            {"key": "learn", "kicker": "4 · THE HIVE LEARNS", "pieces": [
                ("night2_recall", 7.0, learn), ("night2_resolved", 3.0, learn)]},
            {"key": "garden", "kicker": "5 · FIX IT FOR GOOD", "pieces": [
                ("gardener", 3.0, cap["gardener"]), ("night3", 2.0, cap["night3"])]},
        ]
    return [
        {"key": "hook", "kicker": "LEGACY SIEGE · MONGODB IS THE SPINE", "title": True,
         "pieces": [("night1_attack", 7.0, cap["hook"])]},
        {"key": "cascade", "kicker": "1 · CASCADE", "pieces": [("cascade_trace", 10.0, cap["cascade"])]},
        {"key": "lie", "kicker": "2 · THE LYING SYSTEM", "pieces": [("lie_caught", 9.0, cap["lie"])]},
        {"key": "kill", "kicker": "3 · REAL SIGKILL", "pieces": [
            ("approval", 3.0, cap["approval"]), ("kill_takeover", 7.0, cap["kill"])]},
        # {"key": "drift", ...}  schema drift beat goes here after the code freeze (steal ~5 s from "learn")
        {"key": "learn", "kicker": "4 · THE HIVE LEARNS", "pieces": [
            ("night2_recall", 9.0, learn), ("night2_resolved", 3.0, learn)]},
        {"key": "garden", "kicker": "5 · FIX IT FOR GOOD", "pieces": [
            ("gardener", 5.0, cap["gardener"]), ("night3", 3.0, cap["night3"]), ("atlas", 4.0, cap["atlas"])]},
    ]


def resolve(log: dict, total: float, layout: str = "brief") -> list[dict]:
    """Map pieces to source windows; missing beats give their seconds to the rest, scaled to `total`."""
    beats = {b["name"]: b for b in log["beats"]}
    dur = float(log.get("video_duration_s") or 1e9)
    pieces = []
    for seg in plan(log, layout):
        for i, (name, secs, cap) in enumerate(seg["pieces"]):
            b = beats.get(name)
            if not b:
                print(f"  ! beat '{name}' missing from the log, skipped", file=sys.stderr)
                continue
            pieces.append({"seg": seg["key"], "kicker": seg["kicker"], "title": seg.get("title") and i == 0,
                           "beat": name, "slot": secs, "caption": cap, "a": b["t_start"], "b": b["t_end"]})
    scale = total / sum(p["slot"] for p in pieces)
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
    drift = total - sum(p["slot"] for p in pieces)
    pieces[-1]["slot"] = round(pieces[-1]["slot"] + drift, 3)
    return pieces


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.stderr.write(r.stderr[-3000:])
        raise SystemExit(f"ffmpeg failed: {' '.join(cmd[:6])} …")


def esc(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("beats", type=Path, help="beats-<ts>.json written by scripts.record_demo")
    ap.add_argument("--video", type=Path, help="raw video (default: the one named in the beat log)")
    ap.add_argument("--out", type=Path, help="default: legacy-siege-60s.mp4 next to the beat log")
    ap.add_argument("--voiceover", type=Path, help="audio file to mix in (else a silent track)")
    ap.add_argument("--vo-gain", type=float, default=1.0)
    ap.add_argument("--length", type=float, default=60.0)
    ap.add_argument("--layout", choices=["brief", "doc"], default="brief",
                    help="brief: 7/10/9/10/12/12 s ending on Atlas (default); doc: the design doc's 5/15/10/15/10/5 s")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--print-plan", action="store_true")
    a = ap.parse_args()
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not on PATH")
    log = json.loads(a.beats.read_text())
    video = a.video or a.beats.parent / log["video"]
    out = a.out or a.beats.parent / "legacy-siege-60s.mp4"
    pieces = resolve(log, a.length, a.layout)
    t = 0.0
    for p in pieces:
        p["at"] = round(t, 3)
        t += p["slot"]
        print(f"  {p['at']:5.1f}-{t:5.1f}s  {p['beat']:<16} src {p['a']:7.1f}-{p['b']:7.1f}  ×{p['speed']:<5}  {p['caption']}")
    if a.print_plan:
        return 0
    with tempfile.TemporaryDirectory(prefix="cut-", dir=out.parent) as td:
        tmp = Path(td)
        # 1) each piece: seek, speed up to fit its slot, constant 30 fps, near-lossless intermediate
        for i, p in enumerate(pieces):
            p["file"] = tmp / f"p{i:02d}.mp4"
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
        if a.voiceover:
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
