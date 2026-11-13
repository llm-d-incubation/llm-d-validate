"""A shared fleet does not receive the cases that would cost it work or fill its logs.

The behaviour that matters is not just "they do not run" -- it is that the row stays in the
matrix, classified, so a reader can see what the run declined to do. A hand-typed `-k` exclusion
achieves the first and not the second, which is why this is a declaration.
"""

from __future__ import annotations

from tests.fakes import FakeFleet, profile_yaml
from tests.test_suites_against_fake import outcomes, run_suites

DISRUPTIVE = (
    "unhappy.malformed_json",
    "unhappy.missing_required_field",
    "unhappy.wrong_field_type",
    "unhappy.context_overflow",
    "unhappy.invalid_tool_schema",
    "unhappy.invalid_tool_choice",
    "unhappy.streaming_validation",
)

# Well-formed requests a healthy server refuses cheaply. These must keep running everywhere:
# excluding them would cost real coverage for no benefit.
STILL_RUNS = (
    "unhappy.invalid_token_limit",
    "unhappy.unsupported_option",
    "unhappy.unknown_path",
    "unhappy.wrong_method",
)


def write(tmp_path, fleet, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "fake.yaml"
    path.write_text(profile_yaml(fleet.base_url, fleet.model, **kw))
    return path


def test_a_dedicated_fleet_is_the_default_and_runs_everything(tmp_path):
    """An existing profile that says nothing keeps the behaviour it was written against."""
    with FakeFleet() as fleet:
        profile = write(tmp_path, fleet)
        _, records = run_suites(profile, tmp_path / "run", "-k", "unhappy")

    by_case = outcomes(records)
    excluded = [r["case_id"] for r in records if r.get("skip_class") == "excluded_by_policy"]
    assert not excluded, f"nothing should be excluded by default: {excluded}"
    for case in DISRUPTIVE:
        assert by_case.get(case) == "pass", f"{case} -> {by_case.get(case)}"


def test_a_shared_fleet_excludes_the_disruptive_cases(tmp_path):
    with FakeFleet() as fleet:
        profile = write(tmp_path, fleet, fleet_policy="shared")
        _, records = run_suites(profile, tmp_path / "run", "-k", "unhappy")

    by_case = outcomes(records)
    for case in DISRUPTIVE:
        assert by_case.get(case) == "skip", f"{case} should be skipped, got {by_case.get(case)}"


def test_an_excluded_case_keeps_its_row_and_says_why(tmp_path):
    """The point of a class rather than a `-k` exclusion: the run records what it declined."""
    with FakeFleet() as fleet:
        profile = write(tmp_path, fleet, fleet_policy="shared")
        _, records = run_suites(profile, tmp_path / "run", "-k", "malformed_json")

    rows = [r for r in records if r.get("case_id") == "unhappy.malformed_json"]
    assert rows, "the excluded case produced no row at all"
    assert rows[0]["skip_class"] == "excluded_by_policy", rows[0]
    reason = rows[0].get("reason") or ""
    assert "shared" in reason and "cannot parse" in reason, reason


def test_a_shared_fleet_still_runs_the_cheap_refusals(tmp_path):
    """Excluding every unhappy case would be the easy mistake and would cost real coverage."""
    with FakeFleet() as fleet:
        profile = write(tmp_path, fleet, fleet_policy="shared")
        _, records = run_suites(profile, tmp_path / "run", "-k", "unhappy")

    by_case = outcomes(records)
    for case in STILL_RUNS:
        assert by_case.get(case) == "pass", f"{case} -> {by_case.get(case)}"


def test_the_happy_path_suites_are_untouched_by_policy(tmp_path):
    """A shared fleet still gets the ordinary cases; this is not a way of testing less."""
    with FakeFleet() as fleet:
        profile = write(tmp_path, fleet, fleet_policy="shared")
        _, records = run_suites(profile, tmp_path / "run", "-k", "openai_chat or discovery")

    by_case = outcomes(records)
    assert by_case, "no rows at all"
    assert "fail" not in by_case.values(), by_case
    assert not [r for r in records if r.get("skip_class") == "excluded_by_policy"]
