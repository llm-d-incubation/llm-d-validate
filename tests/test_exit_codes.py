"""Every exit code has one class and one meaning, and every place that states them reads them
from exit_codes."""

from __future__ import annotations

import itertools
import re

import pytest

from llmdv.cli import exit_codes, job_cmd
from llmdv.cli.main import build_parser
from llmdv.evidence import dashboard
from tests.fakes import SRC

MANUAL = SRC.parent / "docs" / "manual.md"


def constants() -> dict[str, int]:
    return {
        name: value
        for name, value in vars(exit_codes).items()
        if name.isupper() and isinstance(value, int) and not isinstance(value, bool)
    }


def test_every_exit_code_has_a_class_and_a_meaning():
    codes = constants()
    assert set(codes.values()) == set(exit_codes.EXITS)
    for name, code in codes.items():
        cls, meaning = exit_codes.EXITS[code]
        assert cls and meaning, name


def test_not_delivered_is_a_passed_verification():
    """5 means the verification passed; publication is reported on its own."""
    assert exit_codes.class_of(exit_codes.NOT_DELIVERED) == "ok"


def test_a_code_llmdv_never_returns_is_crashed():
    assert exit_codes.class_of(137) == exit_codes.CRASHED
    assert exit_codes.class_of(None) == exit_codes.CRASHED


def test_the_help_epilog_lists_every_code_and_meaning():
    epilog = build_parser().epilog
    assert epilog == exit_codes.epilog()
    for code, meaning in exit_codes.MEANING.items():
        assert f"  {code}  {meaning}" in epilog


def test_the_manual_exit_table_matches_exit_codes():
    section = MANUAL.read_text().split("## Exit codes", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `(\d+)` \| `(\w+)` \| (.+?) \|$", section, re.MULTILINE)
    assert {int(code): (cls, meaning) for code, cls, meaning in rows} == exit_codes.EXITS


def test_the_dashboard_names_codes_as_exit_codes_does():
    assert dashboard.EXIT_MEANING == {
        code: (exit_codes.class_of(code), meaning) for code, meaning in exit_codes.MEANING.items()
    }


# ----------------------------------------------------------------- llmdv job's precedence

# Every code llmdv returns, plus one it never does: verify killed by an OOM.
JOB_INPUTS = sorted(set(exit_codes.EXITS) | {137})


@pytest.mark.parametrize("verify_code", JOB_INPUTS)
def test_job_exit_precedence_over_every_combination(verify_code):
    """Verification first, then the comparison, then delivery: any failed delivery is 5."""
    for compare_code, publish_code, notify_code in itertools.product(JOB_INPUTS, repeat=3):
        code = job_cmd._exit_code(verify_code, compare_code, publish_code, notify_code)
        inputs = (verify_code, compare_code, publish_code, notify_code)
        if verify_code not in (exit_codes.OK, exit_codes.NOT_DELIVERED):
            expected = verify_code
        elif compare_code in (exit_codes.TEST_FAILURES, exit_codes.USAGE):
            expected = compare_code
        elif publish_code != exit_codes.OK or notify_code != exit_codes.OK:
            expected = exit_codes.NOT_DELIVERED
        else:
            expected = exit_codes.OK
        assert code == expected, inputs
        assert code in exit_codes.EXITS or code == verify_code, inputs
