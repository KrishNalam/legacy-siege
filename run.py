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


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def end_group(pgid: int, grace: float = 8.0, leader: subprocess.Popen | None = None) -> None:
    """SIGTERM a whole process group (a top-level process AND everything it started, e.g. the supervisor's
    agents), wait up to `grace` for it to empty, then SIGKILL what is left. Each child runs in its own
    session, so its pid is its group id even after the leader itself has exited. The leader is reaped while
    waiting: an un-reaped (zombie) leader still counts as a group member."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if leader is not None:
            leader.poll()
        if not group_alive(pgid):
            return
        time.sleep(0.2)
    if group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main() -> None:
    os.chdir(ROOT)
    running: dict[str, subprocess.Popen] = {}
    restarts: dict[str, list[float]] = {name: [] for name, _ in PROCS}

    def stop(*_):
        # every group we ever started (current ones; replaced ones were already ended before relaunch)
        for name, p in list(running.items()):
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for name, p in list(running.items()):
            end_group(p.pid, grace=8.0, leader=p)
        sys.exit(0)

    # cleanup handlers BEFORE anything is launched: a Ctrl-C during startup must not orphan children
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    setup = [PY, "-m", "scripts.setup_db"] + (["--reset"] if "--reset" in sys.argv else [])
    subprocess.run(setup, check=True)

    def launch(name: str, cmd: list[str]) -> None:
        with open(LOGS / f"{name}.log", "a") as log:
            p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        running[name] = p
        print(f"started {name} pid={p.pid} (logs/{name}.log)", flush=True)

    for name, cmd in PROCS:
        launch(name, cmd)
        if name == "systems":
            time.sleep(2)  # systems must be up before agents start probing

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
            # its children may still be alive in its process group (e.g. a crashed supervisor's agents):
            # end that group first, or every restart would add another full set of agents
            if group_alive(p.pid):
                print(f"{name}: ending leftover processes of its old group {p.pid}", flush=True)
                end_group(p.pid)
            print(f"{name} exited with {p.returncode}; restarting", flush=True)
            launch(name, cmd)


if __name__ == "__main__":
    main()
