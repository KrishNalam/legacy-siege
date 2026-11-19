"""Screenshot the /diagram war room at each demo beat while scripts.demo_check drives the demo.

usage: .venv/bin/python -m scripts.shoot OUTDIR     (the stack must be running: run.py)

Shots: idle, night1 (scout tracing), lie (ACK != APPLIED), nurse (takeover after the kill),
recall (night 2), gardener (approval), proven (upgrade proven). Needs Playwright Chromium.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from common import config
from common.db import col

BASE = f"http://127.0.0.1:{config.WARROOM_PORT}"

# (name, condition read from MongoDB, delay before the shot so the screen has caught up)
BEATS = [
    ("night1", lambda: col("incidents").find_one({"diagnosis_path.1": {"$exists": True}}), 1.0),
    ("lie", lambda: col("ledger").find_one({"op": "quarantine_record", "status": "failed"}), 1.0),
    ("nurse", lambda: col("incidents").find_one({"handoffs.0": {"$exists": True}}), 1.5),
    ("recall", lambda: col("incidents").find_one({"recall.used": True}), 2.0),
    ("gardener", lambda: col("approvals").find_one({"kind": "upgrade"}), 0.6),
    ("proven", lambda: col("upgrade_requests").find_one({"status": "proven"}), 1.0),
]


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "shots")
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        br = p.chromium.launch()
        page = br.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(BASE + "/diagram")
        page.wait_for_selector("#ov-nodes > *", timeout=30000)
        drive = subprocess.Popen([sys.executable, "-m", "scripts.demo_check", "--keep"])
        time.sleep(6)  # demo_check's full reset
        page.screenshot(path=str(out / "idle.png"))
        i = 0
        while i < len(BEATS) and drive.poll() is None:
            name, ready, delay = BEATS[i]
            if ready():
                time.sleep(delay)
                page.screenshot(path=str(out / f"{name}.png"))
                print(f"shot {name}", flush=True)
                i += 1
            else:
                time.sleep(0.2)
        code = drive.wait()
        br.close()
    print(f"{i}/{len(BEATS)} beats shot into {out}; demo_check exit {code}")
    return 0 if i == len(BEATS) and code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
