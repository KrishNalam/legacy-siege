"""Old env var names keep working after the plain-names rename (docs/NAMING.md)."""
import importlib


def test_old_names_copy_to_new(monkeypatch):
    monkeypatch.setenv("NURSE_PICKUP_S", "7")
    monkeypatch.setenv("RED_FLEET", "1")
    monkeypatch.setenv("SWARM_DYNAMIC", "1")
    monkeypatch.setenv("WARROOM_VIEW", "flow")
    monkeypatch.setenv("VERIFIER_GRACE_S", "3")
    monkeypatch.setenv("GUARD_GRACE_S", "9")  # new name already set: it wins
    for n in ("RECOVERY_PICKUP_S", "ADVERSARY", "AGENTS_AUTOSCALE", "CONSOLE_VIEW"):
        monkeypatch.delenv(n, raising=False)
    from common import config
    importlib.reload(config)
    import os
    assert os.environ["RECOVERY_PICKUP_S"] == "7" and os.environ["ADVERSARY"] == "1"
    assert os.environ["AGENTS_AUTOSCALE"] == "1" and os.environ["CONSOLE_VIEW"] == "flow"
    assert os.environ["VERIFIER_GRACE_S"] == "3"
    for n in ("RECOVERY_PICKUP_S", "ADVERSARY", "AGENTS_AUTOSCALE", "CONSOLE_VIEW"):
        monkeypatch.delenv(n)


def test_db_names_not_renamed():
    from common.config import _new_env_name
    assert _new_env_name("SWARM_DB") is None and _new_env_name("SWARM_POLL_S") == "AGENTS_POLL_S"
