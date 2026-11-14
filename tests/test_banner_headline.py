"""The closing headline must say what its number counts.

`FAILED | 69 cases` was read as 69 failures when 69 was the total and 7 had failed; the
symmetric `PASSED | 69 cases` hid that 28 of them never ran. The per-outcome lines below the
headline were always right, but the headline is the line people quote into a ticket.
"""

from pathlib import Path

import pytest

from llmdv.cli import banner


def _records(passed: int, failed: int, skipped: int) -> list[dict]:
    out = [{"outcome": "pass", "case_id": f"p{i}"} for i in range(passed)]
    out += [{"outcome": "fail", "case_id": f"f{i}"} for i in range(failed)]
    out += [
        {"outcome": "skip", "skip_class": "excluded_by_policy", "case_id": f"s{i}"}
        for i in range(skipped)
    ]
    return out


def _headline(monkeypatch, records: list[dict]) -> str:
    lines: list[str] = []
    monkeypatch.setattr(banner, "emit", lambda s="": lines.append(s))
    banner.finish(Path("/tmp/does-not-matter"), records, [], 55.1)
    return next(line for line in lines if "PASSED" in line or "FAILED" in line)


def test_a_failing_run_says_how_many_failed_not_how_many_ran(monkeypatch):
    """The regression this file exists for: 7 failed out of 69, not 69 failed."""
    headline = _headline(monkeypatch, _records(34, 7, 28))
    assert "7 of 69" in headline
    assert "FAILED" in headline


def test_a_passing_run_with_skips_does_not_claim_every_case_passed(monkeypatch):
    headline = _headline(monkeypatch, _records(41, 0, 28))
    assert "41 of 69" in headline
    assert "28 skipped" in headline


def test_a_clean_run_with_nothing_skipped_stays_terse(monkeypatch):
    """No '3 of 3': when nothing was skipped the total is the whole story."""
    headline = _headline(monkeypatch, _records(3, 0, 0))
    assert "3 cases passed" in headline
    assert " of " not in headline


def test_one_case_is_not_pluralised(monkeypatch):
    assert "1 case failed" in _headline(monkeypatch, _records(0, 1, 0))


@pytest.mark.parametrize("counts", [(34, 7, 28), (12, 12, 4), (41, 0, 28), (3, 0, 0)])
def test_the_headline_never_shows_a_bare_total_next_to_a_verdict(monkeypatch, counts):
    """A bare total adjacent to FAILED/PASSED is the ambiguity itself."""
    passed, failed, skipped = counts
    headline = _headline(monkeypatch, _records(passed, failed, skipped))
    total = passed + failed + skipped
    if total != passed:
        assert f"{total} cases " not in headline.replace(f"of {total} cases ", "")
