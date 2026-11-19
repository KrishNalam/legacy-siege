"""Syntax-highlighted code cards (1920x1080 PNG) for the CODE segment of the 60 s video (scripts.cut_video).

    python -m scripts.code_cards --out cards/            # render every card, print the paths
    uv run --no-project --with pillow python scripts/code_cards.py --out cards/    # no Pillow in the venv

Each card = file path + ~10-15 real lines taken from the repo at render time (never a copy that can go stale)
+ a one-line plain caption (cut_video burns the caption into the lower third, so the card leaves it free).
A card's file is read from the working tree; if it isn't there yet (e.g. agents/schema.py lives on the
schema-drift branch until that is merged) from `ref` via `git show`. A card whose source can't be found is
skipped, and cut_video hands its seconds to the other cards.

Only the Pillow import is heavy; the CARDS table and `excerpt()` work without it.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONO = "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"
MONO_B = "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"
SANS_B = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
W, H, BAND = 1920, 1080, 168  # cut_video draws its caption band over the bottom BAND px
FONT_PX, MAXCOL = 30, 88  # code font size; soft-wrap column

# Each card: ranges = [(anchor, n_lines)] -> n lines starting at the first line containing `anchor`
# (a "..." gap is drawn between ranges); hl = substrings of the lines to light up; ref = fallback git ref.
CARDS = [
    {"key": "lease", "path": "agents/leases.py", "ref": None,
     "ranges": [("def claim(", 10)], "hl": ["$or", "find_one_and_update", '"$inc"'],
     "caption": "One atomic findOneAndUpdate: only one agent can claim the work"},
    {"key": "verifier", "path": "agents/verify.py", "ref": None,
     "ranges": [("def check(", 2), ('if op == "purge_spool"', 3), ('if op == "restart_cron"', 3)],
     "hl": ["read("],
     "caption": "The Verifier reads the state back. It never trusts the ACK."},
    {"key": "schema", "path": "agents/schema.py", "ref": "origin/t3/add-schema-drift-adaptation",
     "ranges": [("def remembered(", 10)], "hl": ["$elemMatch", "find_one("],
     "caption": "Next rename? MongoDB already remembers the mapping: no AI call"},
]

BG, PANEL, EDGE = (11, 15, 20), (22, 27, 34), (48, 54, 61)
GREEN, WHITE, DIM = (0, 237, 100), (240, 246, 252), (110, 118, 129)
COLORS = {"comment": (139, 148, 158), "string": (165, 214, 255), "number": (121, 192, 255),
          "keyword": (255, 123, 114), "func": (210, 168, 255), "builtin": (255, 166, 87),
          "mongo": (0, 237, 100), "plain": (230, 237, 243)}
KEYWORDS = {"def", "return", "if", "else", "elif", "for", "in", "not", "and", "or", "is", "import", "from", "as",
            "class", "try", "except", "finally", "raise", "with", "lambda", "while", "yield", "pass"}
BUILTINS = {"None", "True", "False", "self", "dict", "list", "str", "int", "float", "sum", "next", "any", "len"}
TOKEN = re.compile(r"""(?P<comment>\#.*)
    |(?P<string>[rbfRBF]{0,2}(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'))
    |(?P<number>\b\d+(?:\.\d+)?\b)
    |(?P<word>[A-Za-z_]\w*)
    |(?P<other>.)""", re.X)


def source(card: dict, root: Path = ROOT) -> tuple[str, str] | None:
    """(text, where) for the card's file: working tree first, else `git show <ref>:<path>`; None if neither."""
    f = root / card["path"]
    if f.exists():
        return f.read_text(), "working tree"
    if card.get("ref"):
        r = subprocess.run(["git", "-C", str(root), "show", f"{card['ref']}:{card['path']}"], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout, card["ref"]
    return None


def excerpt(text: str, ranges) -> list[tuple[int | None, str]]:
    """Rows (source line number, text) picked by (anchor, n) ranges; (None, "") marks a gap between ranges.
    Common indent is removed and alignment-style continuation indents are clamped so lines stay short."""
    lines = text.splitlines()
    rows: list[tuple[int | None, str]] = []
    for anchor, n in ranges:
        i = next((k for k, ln in enumerate(lines) if anchor in ln), None)
        if i is None:
            raise ValueError(f"anchor not found: {anchor!r}")
        if rows:
            rows.append((None, ""))
        rows += [(i + k + 1, ln.rstrip()) for k, ln in enumerate(lines[i:i + n])
                 if not ln.lstrip().startswith('"""')]  # docstrings: the caption says it in plain words
    real = [t for _, t in rows if t]
    cut = min((len(t) - len(t.lstrip()) for t in real), default=0)
    out, prev = [], 0
    for no, t in rows:
        if t:
            ind = min(len(t) - len(t.lstrip()) - cut, prev + 8)
            t, prev = " " * ind + t.lstrip(), ind
        out.append((no, t))
    return out


def wrap(row: str, maxcol: int = MAXCOL) -> list[str]:
    """Soft-wrap one long line at ", " / " " boundaries; continuation lines get +8 indent."""
    out, ind = [], len(row) - len(row.lstrip())
    while len(row) > maxcol:
        cut = max(row.rfind(", ", ind + 8, maxcol), row.rfind(" ", ind + 8, maxcol))
        if cut <= ind:
            cut = maxcol
        keep = cut + 1 if row[cut:cut + 2] == ", " else cut
        out.append(row[:keep].rstrip())
        row = " " * (ind + 8) + row[keep:].lstrip()
    return out + [row]


def tokens(line: str):
    """(text, colour-name) pieces of one line; Mongo operators ("$set", "$jsonSchema") get their own colour."""
    after_def = False
    for m in TOKEN.finditer(line):
        kind, s = m.lastgroup, m.group()
        if kind == "string" and re.match(r"""[rbfRBF]{0,2}["']\$""", s):
            kind = "mongo"
        elif kind == "word":
            kind = "func" if after_def else "keyword" if s in KEYWORDS else "builtin" if s in BUILTINS else "plain"
            after_def = s == "def"
        else:
            kind = kind if kind in ("comment", "string", "number") else "plain"
        yield s, kind


def render(card: dict, rows: list[tuple[int | None, str]], where: str, out: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415 — Pillow only when rendering
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    mono, mono_b = ImageFont.truetype(MONO, FONT_PX), ImageFont.truetype(MONO_B, FONT_PX)
    head, small = ImageFont.truetype(SANS_B, 40), ImageFont.truetype(SANS_B, 26)
    d.text((70, 44), card["path"], font=head, fill=GREEN)
    d.text((W - 70, 52), "the real code, straight from the repo" if where == "working tree" else f"from {where}",
           font=small, fill=DIM, anchor="ra")
    visual: list[tuple[int | None, str, bool]] = []  # (line number or None, text, highlighted)
    for no, t in rows:
        if no is None:
            visual.append((None, "", False))
            continue
        hl = any(h in t for h in card.get("hl", []))
        for k, piece in enumerate(wrap(t)):
            visual.append((no if k == 0 else 0, piece, hl))
    lh, left, top, right = 46, 70, 120, W - 70
    area = H - BAND - 26 - top
    box_h = min(area, lh * len(visual) + 70)
    top += (area - box_h) // 2
    d.rounded_rectangle((left, top, right, top + box_h), radius=18, fill=PANEL, outline=EDGE, width=2)
    cw = d.textlength("M", font=mono)
    gut = len(str(max(n for n, _, _ in visual if n))) * cw
    y = top + (box_h - lh * len(visual)) / 2
    for no, t, hl in visual:
        x0 = left + 40 + gut + 28
        if hl:
            d.rectangle((left + 3, y - 4, right - 3, y + lh - 8), fill=(0, 60, 36))
            d.rectangle((left + 3, y - 4, left + 9, y + lh - 8), fill=GREEN)
        if no is None:
            d.text((x0, y), "...", font=mono, fill=DIM)
        else:
            if no:
                d.text((left + 40 + gut, y), str(no), font=mono, fill=DIM, anchor="ra")
            x = x0
            for piece, kind in tokens(t):
                d.text((x, y), piece, font=mono_b if kind in ("keyword", "func") else mono, fill=COLORS[kind])
                x += d.textlength(piece, font=mono)
            if x > right - 10:
                raise ValueError(f"card '{card['key']}': a line is wider than the card: {t!r}")
        y += lh
    img.save(out)
    return out


def usable(root: Path = ROOT, cards=CARDS) -> list[dict]:
    """The cards whose source file and anchors exist right now (no Pillow needed)."""
    ok = []
    for c in cards:
        src = source(c, root)
        try:
            if src:
                excerpt(src[0], c["ranges"])
                ok.append(c)
        except ValueError:
            pass
    return ok


def build(out_dir: Path, root: Path = ROOT, cards=CARDS) -> list[dict]:
    """Render every card whose source exists -> [{key, png, caption, path, lines}]."""
    out_dir.mkdir(parents=True, exist_ok=True)
    done = []
    for c in cards:
        src = source(c, root)
        if not src:
            print(f"  ! code card '{c['key']}': {c['path']} not found (tree or {c.get('ref')}), skipped", file=sys.stderr)
            continue
        try:
            lines = excerpt(src[0], c["ranges"])  # rows
        except ValueError as e:
            print(f"  ! code card '{c['key']}': {e}, skipped", file=sys.stderr)
            continue
        png = render(c, lines, src[1], out_dir / f"card-{c['key']}.png")
        done.append({"key": c["key"], "png": str(png), "caption": c["caption"], "path": c["path"],
                     "lines": sum(1 for n, _ in lines if n), "from": src[1]})
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--json", action="store_true", help="print the result as JSON (used by cut_video)")
    a = ap.parse_args()
    res = build(a.out)
    if a.json:
        import json  # noqa: PLC0415
        print(json.dumps(res))
    else:
        for r in res:
            print(f"{r['png']}  ({r['path']}, {r['lines']} lines, from {r['from']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
