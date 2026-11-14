"""Start the whole stack: systems (:8701), war room (:8700), swarm supervisor, gardener.

    .venv/bin/python run.py            # Ctrl-C (or SIGTERM) stops everything
    .venv/bin/python run.py --reset    # wipe runtime data first
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from common import config  # load .env before constructing child commands

ROOT = Path(__file__).resolve().parent
PY = sys.executable
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)

PROCS = [
    ("systems", [PY, "-m", "uvicorn", "systems.app:app", "--host", "0.0.0.0", "--port",
                 os.environ.get("SYSTEMS_PORT", "8701"), "--log-level", "warning"]),
    ("warroom", [PY, "-m", "uvicorn", "warroom.server:app", "--host", "0.0.0.0", "--port",
                 os.environ.get("WARROOM_PORT") or os.environ.get("PORT") or "8700", "--log-level", "warning"]),
    ("swarm", [PY, "-m", "swarm.supervisor"]),
    ("gardener", [PY, "-m", "gardener.main"]),
]


def main() -> None:
    os.chdir(ROOT)
    setup = [PY, "-m", "scripts.setup_db"] + (["--reset"] if "--reset" in sys.argv else [])
    subprocess.run(setup, check=True)
    running: dict[str, subprocess.Popen] = {}
    restarts: dict[str, list[float]] = {name: [] for name, _ in PROCS}

    def launch(name: str, cmd: list[str]) -> None:
        with open(LOGS / f"{name}.log", "a") as log:
            p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        running[name] = p
        print(f"started {name} pid={p.pid} (logs/{name}.log)", flush=True)

    for name, cmd in PROCS:
        launch(name, cmd)
        if name == "systems":
            time.sleep(2)  # systems must be up before agents start probing

    def stop(*_):
        for name, p in running.items():
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Crash-only design: any process may die (chaos kills agents on purpose); state is in MongoDB,
    # so a crashed top-level process is simply restarted. Give up only on a tight crash loop.
    while True:
        time.sleep(2)
        for name, cmd in PROCS:
            p = running[name]
            if p.poll() is None:
                continue
            recent = [t for t in restarts[name] if time.monotonic() - t < 60]
            restarts[name] = recent + [time.monotonic()]
            if len(recent) >= 5:
                print(f"{name} crash-looping (exit {p.returncode}); see logs/{name}.log — stopping", flush=True)
                stop()
            print(f"{name} exited with {p.returncode}; restarting", flush=True)
            launch(name, cmd)


if __name__ == "__main__":
    main()
