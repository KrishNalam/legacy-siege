"""The Test step: run the system's own tests + a regression replay against a patched sandbox.

Nothing here touches the real `systems/` files or the live `world` DB:
  * the whole systems package is copied to a temp dir and the patched module is
    written into the copy;
  * pytest and the replay run as subprocesses with cwd = the sandbox, so
    `import systems.<sys>` resolves to the patched copy;
  * the replay boots the app against WORLD_DB=world_test_gardener, dropped afterwards.

GARDENER_SANDBOX_URI (optional) points that throwaway CI database at a different MongoDB, e.g. a
local atlas-local container. Live state stays on MONGODB_URI either way; on Atlas, creating and
dropping the sandbox's collections/indexes per test costs ~70 s versus ~4 s locally.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from common import config
from common.db import client

REPLAY_WORLD_DB = os.environ.get("GARDENER_REPLAY_WORLD_DB", "world_test_gardener")
REPLAY_SWARM_DB = os.environ.get("GARDENER_REPLAY_SWARM_DB", "swarm_test_gardener")
_SANDBOX_URI_CFG = os.environ.get("GARDENER_SANDBOX_URI") or None
SANDBOX_URI: str | None = None  # resolved per run: None = same cluster as live state


def _resolve_sandbox() -> str | None:
    """Use GARDENER_SANDBOX_URI only if it answers a ping now; otherwise fall back to the live cluster
    (the box may have stopped an idle local container — slower, never a failed upgrade)."""
    if not _SANDBOX_URI_CFG:
        return None
    from pymongo import MongoClient

    try:
        c = MongoClient(_SANDBOX_URI_CFG, serverSelectionTimeoutMS=1500)
        try:
            c.admin.command("ping")
        finally:
            c.close()
        return _SANDBOX_URI_CFG
    except Exception:
        return None
PYTEST_TIMEOUT_S = 180
REPLAY_TIMEOUT_S = 90
MAX_OUTPUT = 6000  # chars kept per section in the upgrade doc


def _env() -> dict:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(config.ROOT),
        "WORLD_DB": REPLAY_WORLD_DB,
        "SWARM_DB": REPLAY_SWARM_DB,
        "PACE": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    if SANDBOX_URI:
        env["MONGODB_URI"] = SANDBOX_URI
    return env


def _tail(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else "...\n" + text[-MAX_OUTPUT:]


def _run(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, env=_env(), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out + f"\nTIMEOUT after {timeout}s"


def _summary(out: str) -> str:
    """One line for the rejection reason: the 'missing ...' message or pytest's final tally."""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("missing "):
            return ln.split(":")[0]
    tail = [ln for ln in lines if not ln.startswith("REPLAY_RESULT")]
    return (tail[-1] if tail else "no output")[:160]


def make_sandbox(systems_dir: Path, system: str, patched_source: str) -> tempfile.TemporaryDirectory:
    """Temp dir containing `systems/` (a copy of systems_dir) with the patched module written in."""
    tmp = tempfile.TemporaryDirectory(prefix="gardener-sandbox-")
    dst = Path(tmp.name) / "systems"
    shutil.copytree(systems_dir, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
    (dst / f"{system}.py").write_text(patched_source)
    return tmp


def run_pytest(sandbox: Path, system: str) -> tuple[bool, str]:
    test_file = Path("systems") / "tests" / f"test_{system}.py"
    if not (sandbox / test_file).exists():
        return False, f"missing {test_file}: refusing to ship an upgrade without its test file"
    code, out = _run([sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider",
                      "--rootdir", str(sandbox)], sandbox, PYTEST_TIMEOUT_S)
    return code == 0, f"$ pytest {test_file}  (exit {code})\n{out}"


def run_replay(sandbox: Path, system: str) -> tuple[bool, str]:
    code, out = _run([sys.executable, "-m", "gardener.replay", system], sandbox, REPLAY_TIMEOUT_S)
    return code == 0, f"$ regression replay (batch_abort, WORLD_DB={REPLAY_WORLD_DB})  (exit {code})\n{out}"


def sandbox_label() -> str:
    """Where the sandbox ran, for the upgrade doc / war room (never includes credentials)."""
    if not SANDBOX_URI:
        return "same cluster"
    return "local sandbox" if ("localhost" in SANDBOX_URI or "127.0.0.1" in SANDBOX_URI) else "separate cluster"


def drop_replay_dbs() -> None:
    from pymongo import MongoClient

    c = MongoClient(SANDBOX_URI, serverSelectionTimeoutMS=5000) if SANDBOX_URI else client()
    try:
        for name in (REPLAY_WORLD_DB, REPLAY_SWARM_DB):
            if name.startswith(("world_test", "swarm_test")):  # never the live DBs
                c.drop_database(name)
    finally:
        if SANDBOX_URI:
            c.close()


def run_tests(system: str, patched_source: str, systems_dir: Path) -> dict:
    """Returns {passed, output, pytest_passed, replay_passed} (output is truncated for the doc)."""
    global SANDBOX_URI
    SANDBOX_URI = _resolve_sandbox()
    tmp = make_sandbox(systems_dir, system, patched_source)
    try:
        sandbox = Path(tmp.name)
        py_ok, py_out = run_pytest(sandbox, system)
        rp_ok, rp_out = run_replay(sandbox, system)
    finally:
        tmp.cleanup()
        try:
            drop_replay_dbs()
        except Exception:  # pragma: no cover - cleanup is best effort
            pass
    return {
        "passed": py_ok and rp_ok,
        "summary": f"pytest: {_summary(py_out)}; replay: {'pass' if rp_ok else _summary(rp_out)}",
        "pytest_passed": py_ok,
        "replay_passed": rp_ok,
        "sandbox": sandbox_label(),
        "output": _tail(py_out) + "\n\n" + _tail(rp_out),
    }
