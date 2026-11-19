"""The fixture system's own small test file (what the Upgrader's Test step runs)."""
from systems import mainframe


def rec(account: str, amount: str) -> str:
    return (account.rjust(10, "0") + amount.rjust(12)).ljust(mainframe.RECORD_LEN)


def test_good_batch_posts_totals():
    out = mainframe.run_batch([rec("1", "+00000000010"), rec("2", "00000000005-")])
    assert out == {"status": "OK", "total": 5, "rejected": []}


def test_invalid_records_are_skipped(monkeypatch):
    monkeypatch.setattr(mainframe, "validate_record", lambda r: "XX" not in r)
    out = mainframe.run_batch([rec("9", "XX"), rec("1", "+00000000010")])
    assert out["rejected"] == [0] and out["status"] == "OK"
