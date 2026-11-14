"""Minimal legacy mainframe for Gardener tests. Follows the validate_record contract.

NIGHTBAT: records are fixed-width 80-char lines; a record that passes validation but
carries non-numeric data in the zoned amount field ABENDs the batch (S0C7).
"""
from __future__ import annotations

RECORD_LEN = 80
ACCOUNT_FIELD = slice(0, 10)
AMOUNT_FIELD = slice(10, 22)  # zoned decimal, optional leading/trailing sign


class Abend(Exception):
    """The batch abended (code in args[0])."""


def validate_record(record: str) -> bool:
    """Legacy: no validation. Every record is accepted into NIGHTBAT."""
    return True


def parse_amount(field: str) -> int:
    s = field.strip()
    if s[-1:] in ("+", "-"):
        s = s[-1] + s[:-1]
    if not s.lstrip("+-").isdigit():
        raise Abend("S0C7")
    return int(s)


def run_batch(records: list[str]) -> dict:
    """Post every valid record; invalid ones are rejected, the batch continues."""
    total, rejected = 0, []
    for i, rec in enumerate(records):
        if not validate_record(rec):
            rejected.append(i)
            continue
        total += parse_amount(rec[AMOUNT_FIELD])  # raises Abend on poison
    return {"status": "OK", "total": total, "rejected": rejected}
