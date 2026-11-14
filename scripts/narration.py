"""Offline text-to-speech narration for the 60 s video (scripts.cut_video --tts). No root, no cloud, no API key.

The text comes from docs/video-script.md: the "Plain text (for TTS)" section, one paragraph per segment,
each tagged `<!-- vo:<segment key> -->` on the line above it. Engines, first one that works wins:

  piper   neural voice (good). A private venv with `piper-tts` + one voice model, set up once:
              uv venv ~/projects/legacy-siege-media/tts/piper-venv
              uv pip install --python ~/projects/legacy-siege-media/tts/piper-venv/bin/python piper-tts
              cd ~/projects/legacy-siege-media/tts && for f in en_US-lessac-medium.onnx{,.json}; do
                curl -sSLO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/$f; done
          Override the location with $LEGACY_SIEGE_TTS_DIR (default ~/projects/legacy-siege-media/tts).
  espeak  `espeak-ng` on PATH (robotic).
  flite   ffmpeg's libflite filter, voice slt (robotic, but ships inside most ffmpeg builds).

    python -m scripts.narration --list                  # which engines are usable here
    python -m scripts.narration --out vo/ [--engine piper]   # one wav per segment + durations
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "docs" / "video-script.md"
TTS_DIR = Path(os.environ.get("LEGACY_SIEGE_TTS_DIR", "~/projects/legacy-siege-media/tts")).expanduser()
VOICE = "en_US-lessac-medium"
ENGINES = ("piper", "espeak", "flite")


def paragraphs(md: Path = SCRIPT) -> dict[str, str]:
    """{segment key: plain text} from the `<!-- vo:key -->` tagged paragraphs of the script."""
    text = md.read_text()
    out = {}
    for key, body in re.findall(r"<!--\s*vo:(\w+)\s*-->\s*\n(.+?)(?:\n\s*\n|\Z)", text, re.S):
        out[key] = " ".join(body.split())
    if not out:
        raise SystemExit(f"{md}: no `<!-- vo:key -->` paragraphs found")
    return out


def piper_cmd() -> list[str] | None:
    py, model = TTS_DIR / "piper-venv" / "bin" / "python", TTS_DIR / f"{VOICE}.onnx"
    if py.exists() and model.exists():
        return [str(py), "-m", "piper", "-m", str(model)]
    return None


def flite_ok() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    return " flite " in r.stdout


def available() -> list[str]:
    ok = []
    if piper_cmd():
        ok.append("piper")
    if shutil.which("espeak-ng"):
        ok.append("espeak")
    if flite_ok():
        ok.append("flite")
    return ok


def synth(text: str, wav: Path, engine: str) -> Path:
    """Synthesise `text` into `wav` (any sample rate; cut_video resamples)."""
    if engine == "piper":
        cmd = piper_cmd()
        r = subprocess.run([*cmd, "-f", str(wav), "--length-scale", "1.0"], input=text, text=True,
                           capture_output=True)
    elif engine == "espeak":
        r = subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", str(wav), text], capture_output=True, text=True)
    elif engine == "flite":
        tf = wav.with_suffix(".txt")
        tf.write_text(text)
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"flite=textfile={tf}:voice=slt",
                            str(wav)], capture_output=True, text=True)
    else:
        raise SystemExit(f"unknown TTS engine {engine!r}")
    if r.returncode or not wav.exists() or wav.stat().st_size < 1000:
        raise SystemExit(f"TTS ({engine}) failed: {(r.stderr or r.stdout)[-400:]}")
    return wav


def duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def narrate(out_dir: Path, engine: str | None = None, md: Path = SCRIPT) -> tuple[str, dict[str, tuple[Path, float]]]:
    """Synthesise every paragraph -> (engine used, {key: (wav, seconds)})."""
    have = available()
    if engine:
        if engine not in have:
            raise SystemExit(f"TTS engine {engine!r} not usable here (have: {have or 'none'})")
    elif have:
        engine = have[0]
    else:
        raise SystemExit("no TTS engine found: set up piper (see scripts/narration.py), or "
                         "`box request install espeak-ng --why 'TTS narration for hackathon video'`")
    out_dir.mkdir(parents=True, exist_ok=True)
    res = {}
    for key, text in paragraphs(md).items():
        wav = synth(text, out_dir / f"vo-{key}.wav", engine)
        res[key] = (wav, duration(wav))
    return engine, res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--engine", choices=ENGINES)
    a = ap.parse_args()
    if a.list or not a.out:
        print("usable TTS engines:", ", ".join(available()) or "none")
        return 0
    engine, res = narrate(a.out, a.engine)
    for k, (p, s) in res.items():
        print(f"{engine:<7} {k:<8} {s:5.1f} s  {len(paragraphs()[k].split()):>3} words  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
