"""Regression replay logic: fails on the legacy module, passes on the patched one (unit + app layers)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from gardener import testing
from gardener.remediations import batch_validation as bv
from gardener import patcher

FIX = Path(__file__).parent / "fixtures"


def patched(src: str) -> str:
    tree = patcher.parse(src)
    site = patcher.locate_function(src, "validate_record", tree)
    return patcher.replace_body(src, site, bv.render_body(site.arg_name, bv.detect_layout(tree)))


@pytest.fixture
def systems_dir(tmp_path) -> Path:
    d = tmp_path / "systems"
    shutil.copytree(FIX / "systems", d)
    return d


def test_replay_fails_on_legacy_passes_on_patched(systems_dir):
    legacy = (systems_dir / "mainframe.py").read_text()
    for src, expect in ((legacy, False), (patched(legacy), True)):
        tmp = testing.make_sandbox(systems_dir, "mainframe", src)
        try:
            ok, out = testing.run_replay(Path(tmp.name), "mainframe")
        finally:
            tmp.cleanup()
        assert ok is expect, out
        assert "app replay: skipped" in out


def test_app_replay_against_contract_api(systems_dir):
    shutil.copy(FIX / "fake_app.py", systems_dir / "app.py")
    legacy = (systems_dir / "mainframe.py").read_text()
    results = {}
    for name, src in (("legacy", legacy), ("patched", patched(legacy))):
        tmp = testing.make_sandbox(systems_dir, "mainframe", src)
        try:
            results[name] = testing.run_replay(Path(tmp.name), "mainframe")
        finally:
            tmp.cleanup()
    ok, out = results["patched"]
    assert ok, out
    assert "app replay: batch OK" in out and "rejected-record evidence=yes" in out
    ok, out = results["legacy"]
    assert not ok and "ABEND=S0C7" in out


def test_run_tests_runs_system_pytest_and_replay(systems_dir):
    res = testing.run_tests("mainframe", patched((systems_dir / "mainframe.py").read_text()), systems_dir)
    assert res["passed"] and res["pytest_passed"] and res["replay_passed"], res["output"]
    assert "2 passed" in res["output"]
    assert (systems_dir / "mainframe.py").read_text().count("return True") == 1  # original untouched


def test_missing_test_file_fails(systems_dir):
    (systems_dir / "tests" / "test_mainframe.py").unlink()
    res = testing.run_tests("mainframe", patched((systems_dir / "mainframe.py").read_text()), systems_dir)
    assert not res["passed"] and "missing" in res["output"]
