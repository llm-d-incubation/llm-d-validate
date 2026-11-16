"""Every exit code has one class and one meaning, and every place that states them reads them
from exit_codes."""

from __future__ import annotations

import re

from llmdv.cli import exit_codes
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
