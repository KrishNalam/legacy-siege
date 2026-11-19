"""AST patch is minimal and correct; remediation layout detection; diff size guardrail."""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from upgrader import patcher
from upgrader.remediations import batch_validation as bv

FIXTURE = Path(__file__).parent / "fixtures" / "systems" / "mainframe.py"


def patch_source(src: str) -> tuple[str, patcher.FunctionSite, bv.Layout]:
    tree = patcher.parse(src)
    site = patcher.locate_function(src, "validate_record", tree)
    layout = bv.detect_layout(tree)
    return patcher.replace_body(src, site, bv.render_body(site.arg_name, layout)), site, layout


def load(src: str) -> types.ModuleType:
    mod = types.ModuleType("patched_mainframe")
    exec(compile(src, "patched_mainframe.py", "exec"), mod.__dict__)
    return mod


def test_patch_only_touches_the_function_body():
    src = FIXTURE.read_text()
    out, site, layout = patch_source(src)
    assert not patcher.changed_outside(src, out, site)
    before, after = src.splitlines(), out.splitlines()
    assert before[: site.body_start - 1] == after[: site.body_start - 1]  # incl. the def line
    assert before[site.end_line:] == after[len(after) - (len(before) - site.end_line):]
    assert after[site.def_line - 1] == "def validate_record(record: str) -> bool:"
    assert layout.source == "module"
    assert "record[AMOUNT_FIELD]" in out and "len(record) != RECORD_LEN" in out  # reuses the module's names


def test_diff_is_small_and_reviewable():
    src = FIXTURE.read_text()
    out, _, _ = patch_source(src)
    diff = patcher.unified_diff(src, out, "systems/mainframe.py")
    assert 0 < patcher.diff_line_count(diff) < patcher.MAX_DIFF_LINES
    assert diff.startswith("--- a/systems/mainframe.py\n+++ b/systems/mainframe.py\n")
    removed = [ln for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")]
    assert removed == ['-    """Legacy: no validation. Every record is accepted into NIGHTBAT."""', "-    return True"]


def test_patched_validate_record_semantics():
    src = FIXTURE.read_text()
    out, _, layout = patch_source(src)
    legacy, mod = load(src), load(out)
    samples = bv.sample_records(layout)
    assert all(legacy.validate_record(r) for r in samples["bad"])  # the weakness
    assert all(mod.validate_record(r) is True for r in samples["good"])
    assert all(mod.validate_record(r) is False for r in samples["bad"])
    assert mod.validate_record(None) is False


def test_patched_batch_survives_poison_legacy_abends():
    src = FIXTURE.read_text()
    out, _, layout = patch_source(src)
    good, poison = bv.sample_records(layout)["good"][0], bv.sample_records(layout)["bad"][0]
    with pytest.raises(Exception, match="S0C7"):
        load(src).run_batch([good, poison])
    res = load(out).run_batch([good, poison])
    assert res["status"] == "OK" and res["rejected"] == [1] and res["total"] == 1234


GENERIC = '''import os

# a legacy module with no layout constants
def helper():
    return 1


def validate_record(rec):
    # legacy comment kept
    return True
'''


def test_generic_fallback_uses_default_layout_and_keeps_comments():
    out, site, layout = patch_source(GENERIC)
    assert layout.source == "default" and layout.length == 80
    assert "rec[slice(10, 22)]" in out and "len(rec) != 80" in out
    assert "    # legacy comment kept\n" in out
    assert not patcher.changed_outside(GENERIC, out, site)
    mod = load(out)
    s = bv.sample_records(layout)
    assert all(mod.validate_record(r) for r in s["good"]) and not any(mod.validate_record(r) for r in s["bad"])


def test_layout_detection_variants():
    src = ('RECORD_LENGTH = 80\nLAYOUT = {"acct_no": (2, 12), "amount": slice(40, 52)}\n'
           "def validate_record(r):\n    return True\n")
    lay = bv.detect_layout(patcher.parse(src))
    assert lay.length_expr == "RECORD_LENGTH"
    assert lay.field_expr == {"account": "slice(*LAYOUT['acct_no'])", "amount": "LAYOUT['amount']"}
    assert lay.cols == {"account": (2, 12), "amount": (40, 52)}
    src2 = "REC_LEN = 80\nACCT_START = 0\nACCT_END = 8\nAMT_START = 20\nAMT_LEN = 11\n"
    lay2 = bv.detect_layout(patcher.parse(src2))
    assert lay2.field_expr == {"account": "slice(ACCT_START, ACCT_END)", "amount": "slice(AMT_START, AMT_START + AMT_LEN)"}
    assert lay2.cols["amount"] == (20, 31)


def test_missing_or_ambiguous_function_fails_safely():
    with pytest.raises(patcher.PatchError, match="not found"):
        patcher.locate_function("def other(x):\n    return 1\n", "validate_record")
    with pytest.raises(patcher.PatchError, match="2 times"):
        patcher.locate_function("def validate_record(x):\n    return 1\ndef validate_record(x):\n    return 2\n",
                                "validate_record")
    with pytest.raises(patcher.PatchError, match="does not parse"):
        patcher.parse("def broken(:\n")


def test_already_applied_is_detected():
    out, _, _ = patch_source(FIXTURE.read_text())
    site = patcher.locate_function(out, "validate_record")
    assert bv.already_applied(site.docstring)


def test_file_without_trailing_newline_and_crlf():
    src = "def validate_record(r):\r\n    return True"
    out, site, _ = patch_source(src)
    assert not out.endswith("\n") and "\r\n    if not isinstance(r, str)" in out
    load(out)
