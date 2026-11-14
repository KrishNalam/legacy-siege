"""Remediation `batch_validation`: validate NIGHTBAT fixed-width records before the batch runs.

The weakness (CONTRACTS §2/§3, fault class `batch_abort`): `systems/mainframe.py`
ships with

    def validate_record(record: str) -> bool:
        return True            # legacy: every record is accepted

so a record with non-numeric data in the zoned amount field reaches the batch and
ABENDs it (S0C7). The batch already skips records for which validate_record() is
False, so the permanent fix is a new *body* for that one function:

    * length must be exactly RECORD_LEN (80),
    * the account field must be all digits,
    * the amount field must be digits with an optional leading or trailing sign.

The fix is predetermined and honest: this module renders the body; the LLM (if any)
only writes the human explanation. The record layout is read from the target
module's own constants when they exist (RECORD_LEN, AMOUNT_FIELD = slice(...), a
LAYOUT dict, ..._START/_END pairs), so the patch references the system's names
instead of hard-coding columns. Otherwise it falls back to DEFAULT_LAYOUT.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

NAME = "batch_validation"
SYSTEM = "mainframe"
FUNCTION = "validate_record"
FAULT_CLASSES = {"batch_abort", "poison_record", "abend_s0c7", "s0c7", "batch"}
MARKER = "Gardener remediation batch_validation"  # in the new docstring; marks a file as already upgraded

RECORD_LEN_DEFAULT = 80
# Fallback layout (0-based, end-exclusive) when the mainframe module exposes no constants.
DEFAULT_LAYOUT = {"account": (0, 10), "amount": (10, 22)}

_LEN_NAMES = ("RECORD_LEN", "RECORD_LENGTH", "REC_LEN", "RECLEN", "LRECL", "RECORD_SIZE")
_FIELD_RE = {"account": re.compile(r"ACC(OUN)?T|ACCT"), "amount": re.compile(r"AMOUNT|AMT")}


@dataclass
class Layout:
    """How the remediation refers to the record length and to the two fields it checks.

    `*_expr` are Python expressions (strings) evaluated inside the patched function,
    e.g. "RECORD_LEN", "80", "AMOUNT_FIELD", "slice(10, 22)". `cols` holds the resolved
    numbers so the replay can build sample records.
    """

    length_expr: str = str(RECORD_LEN_DEFAULT)
    length: int = RECORD_LEN_DEFAULT
    field_expr: dict[str, str] = field(default_factory=dict)
    cols: dict[str, tuple[int, int]] = field(default_factory=dict)
    source: str = "default"      # "module" when every value came from the target's constants
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ layout detection
def _int(node: ast.AST) -> int | None:
    try:
        v = ast.literal_eval(node)
    except Exception:
        return None
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _span(node: ast.AST) -> tuple[tuple[int, int], str] | None:
    """(start, end) and a 'kind' for `slice(a, b)` → 'slice' or `(a, b)` → 'tuple'."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "slice" \
            and len(node.args) == 2 and not node.keywords:
        a, b = _int(node.args[0]), _int(node.args[1])
        if a is not None and b is not None and 0 <= a < b:
            return (a, b), "slice"
    if isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == 2:
        a, b = _int(node.elts[0]), _int(node.elts[1])
        if a is not None and b is not None and 0 <= a < b:
            return (a, b), "tuple"
    return None


def _module_constants(tree: ast.Module) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            out[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            out[node.target.id] = node.value
    return out


def detect_layout(tree: ast.Module) -> Layout:
    """Read RECORD_LEN and the account/amount field positions from module-level constants."""
    consts = _module_constants(tree)
    lay = Layout()
    for n in _LEN_NAMES:
        if n in consts and _int(consts[n]):
            lay.length_expr, lay.length = n, _int(consts[n])
            break
    else:
        lay.notes.append(f"no record-length constant; using {RECORD_LEN_DEFAULT}")

    for fname, rx in _FIELD_RE.items():
        found = _field_from_names(consts, rx) or _field_from_dict(consts, fname)
        if found:
            lay.field_expr[fname], lay.cols[fname] = found
        else:
            a, b = DEFAULT_LAYOUT[fname]
            lay.field_expr[fname], lay.cols[fname] = f"slice({a}, {b})", (a, b)
            lay.notes.append(f"no {fname} field constant; using default cols {a}:{b}")
    if not lay.notes:
        lay.source = "module"
    elif len(lay.notes) < 3:
        lay.source = "partial"
    return lay


def _field_from_names(consts: dict[str, ast.AST], rx: re.Pattern) -> tuple[str, tuple[int, int]] | None:
    # AMOUNT_FIELD = slice(30, 42)  /  AMOUNT = (30, 42)
    for name, node in consts.items():
        if name.isupper() and rx.search(name):
            sp = _span(node)
            if sp:
                cols, kind = sp
                return (name if kind == "slice" else f"slice(*{name})"), cols
    # AMOUNT_START = 30 ; AMOUNT_END = 42 (or AMOUNT_LEN / AMOUNT_WIDTH)
    for name, node in consts.items():
        if name.isupper() and rx.search(name) and name.endswith(("_START", "_POS", "_OFFSET")):
            stem, start = name.rsplit("_", 1)[0], _int(node)
            if start is None:
                continue
            for suffix in ("_END", "_STOP"):
                end = _int(consts.get(stem + suffix, ast.Constant(None)))
                if end is not None and end > start:
                    return f"slice({name}, {stem + suffix})", (start, end)
            for suffix in ("_LEN", "_WIDTH", "_LENGTH"):
                width = _int(consts.get(stem + suffix, ast.Constant(None)))
                if width:
                    return f"slice({name}, {name} + {stem + suffix})", (start, start + width)
    return None


def _field_from_dict(consts: dict[str, ast.AST], fname: str) -> tuple[str, tuple[int, int]] | None:
    # LAYOUT = {"account": slice(0, 10), "amount": (10, 22), ...}
    for name, node in consts.items():
        if not (name.isupper() and isinstance(node, ast.Dict)):
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str) and _FIELD_RE[fname].search(k.value.upper()):
                sp = _span(v)
                if sp:
                    cols, kind = sp
                    ref = f"{name}[{k.value!r}]"
                    return (ref if kind == "slice" else f"slice(*{ref})"), cols
    return None


# ------------------------------------------------------------------ the new body
def render_body(arg: str, layout: Layout) -> str:
    """The replacement body for validate_record (unindented; the patcher indents it)."""
    return f'''\
"""{MARKER}: reject malformed NIGHTBAT records.

Fixed-width record of {layout.length_expr} chars, numeric account, signed numeric amount.
A bad record is rejected here instead of ABENDing the batch (S0C7).
"""
if not isinstance({arg}, str) or len({arg}) != {layout.length_expr}:
    return False
account = {arg}[{layout.field_expr["account"]}].strip()
amount = {arg}[{layout.field_expr["amount"]}].strip()
if not (account.isascii() and account.isdigit()):
    return False
if amount[:1] in ("+", "-"):
    amount = amount[1:]
elif amount[-1:] in ("+", "-"):
    amount = amount[:-1]
return amount.isascii() and amount.isdigit()
'''


def already_applied(docstring: str | None) -> bool:
    return bool(docstring and MARKER in docstring)


# ------------------------------------------------------------------ replay samples
def sample_records(layout: Layout) -> dict[str, list[str]]:
    """Records the regression replay feeds to the patched validate_record().

    `good` must be accepted, `bad` must be rejected (the S0C7 poison + length errors).
    """
    def build(account: str, amount: str, length: int | None = None) -> str:
        rec = [" "] * layout.length
        for fname, text in (("account", account), ("amount", amount)):
            a, b = layout.cols[fname]
            width = b - a
            rec[a:b] = list(text.rjust(width, "0") if text.isdigit() else text.ljust(width)[:width])
        s = "".join(rec)[: layout.length]
        return s if length is None else (s + " " * length)[:length]

    aw = layout.cols["amount"][1] - layout.cols["amount"][0]
    acw = layout.cols["account"][1] - layout.cols["account"][0]
    pos = "+" + "1234".rjust(aw - 1, "0")
    neg = "1234".rjust(aw - 1, "0") + "-"
    good = [build("4211", pos), build("4211", neg), build("4211", "1234")]
    bad = [
        build("4211", "12A4".rjust(aw, "0")),           # S0C7: non-numeric in the amount
        build("4211", " " * aw),                        # blank amount
        build("42X1".rjust(acw, "0"), pos),             # non-numeric account
        build("4211", pos, layout.length - 1),          # short record
        build("4211", pos) + "X",                       # long record
    ]
    return {"good": good, "bad": bad}
