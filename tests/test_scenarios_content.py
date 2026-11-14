"""Scenario content cases, each proven to fail on the defect it targets."""

import pytest
import yaml

from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile

SURFACES = ["chat_completions", "messages", "responses"]


def run_blank(tmp_path, surface, *, declared=True, **fleet_kw):
    with FakeFleet(**fleet_kw) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = [surface]
        if declared:
            raw["capabilities"]["scenario_checks"] = ["blank_turns"]
        path.write_text(yaml.safe_dump(raw))
        return run_suites(
            path, tmp_path / "run", "-k", "test_blank_turns", "--llmdv-surface", surface
        )


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("defect,outcome", [(False, "pass"), (True, "fail")])
def test_blank_turns_never_get_a_server_error(tmp_path, surface, defect, outcome):
    code, records = run_blank(tmp_path, surface, blank_turn_error=defect)
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


def test_blank_turns_skip_where_undeclared(tmp_path):
    code, records = run_blank(tmp_path, "chat_completions", declared=False)
    assert code == 0 and records[0]["outcome"] == "skip", records


def test_blank_turns_do_not_apply_to_completions(tmp_path):
    code, records = run_blank(tmp_path, "completions")
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "[not_implemented]" in records[0]["reason"], records
