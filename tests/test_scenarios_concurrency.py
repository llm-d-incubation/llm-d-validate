"""The cancel burst case, proven to fail on the defect it targets."""

import pytest

from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile

SURFACES = ["chat_completions", "messages", "responses"]


def run_burst(tmp_path, surface, select="test_cancel_burst", policy=None, **fleet_kw):
    with FakeFleet(**fleet_kw) as fleet:
        path = write_profile(tmp_path, fleet, responses=True, fleet_policy=policy)
        code, records = run_suites(path, tmp_path / "run", "-k", select, "--llmdv-surface", surface)
        return code, records, fleet


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("defect,outcome", [(False, "pass"), (True, "fail")])
def test_cancel_burst_needs_the_path_to_keep_serving(tmp_path, surface, defect, outcome):
    code, records, _ = run_burst(tmp_path, surface, burst_wedge=defect)
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records
    if defect:
        assert "503" in records[0]["reason"], records


def test_single_disconnect_misses_a_burst_wedge(tmp_path):
    code, records, _ = run_burst(
        tmp_path, "chat_completions", "test_disconnect_recovery_chat", burst_wedge=True
    )
    assert code == 0 and records[0]["outcome"] == "pass", records


def test_cancel_burst_sends_the_whole_burst(tmp_path):
    code, records, fleet = run_burst(tmp_path, "chat_completions")
    assert code == 0, records
    posts = [path for method, path, _ in fleet.seen if method == "POST"]
    assert posts.count("/v1/chat/completions") == 9, posts


def test_cancel_burst_is_excluded_on_shared_fleets(tmp_path):
    code, records, _ = run_burst(tmp_path, "chat_completions", policy="shared")
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "[excluded_by_policy]" in records[0]["reason"], records
