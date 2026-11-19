"""The launcher must honour .env ports before it creates child commands."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_launcher_reads_dotenv_ports(tmp_path):
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "__init__.py").touch()
    shutil.copyfile(root / "common" / "config.py", tmp_path / "common" / "config.py")
    shutil.copyfile(root / "run.py", tmp_path / "run.py")
    (tmp_path / ".env").write_text("SYSTEMS_PORT=19231\nWARROOM_PORT=19232\n")
    env = {k: v for k, v in os.environ.items() if k not in {"SYSTEMS_PORT", "CONSOLE_PORT", "PORT", "PYTHONPATH"}}
    result = subprocess.run(
        [sys.executable, "-c", "import json, run; print(json.dumps(dict(run.PROCS)))"],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True, timeout=10,
    )
    commands = json.loads(result.stdout)
    assert commands["systems"][commands["systems"].index("--port") + 1] == "19231"
    assert commands["console"][commands["console"].index("--port") + 1] == "19232"
