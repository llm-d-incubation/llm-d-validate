import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize(
    "case,defect,outcome",
    [
        ("retrieval", None, "pass"),
        ("retrieval", "missing", "fail"),
        ("retrieval", "changed_id", "fail"),
        ("retrieval", "changed_output", "fail"),
        ("cancellation", None, "pass"),
        ("cancellation", "ignored_cancel", "fail"),
        ("cancellation", "not_persisted", "fail"),
        ("cancellation", "fast", "skip"),
        ("cancellation", "race", "skip"),
    ],
)
def test_response_state_contract(tmp_path, case, defect, outcome):
    with FakeFleet(stateful_responses=True, response_state_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"] = {"stateful_responses": True, "background_responses": True}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", f"test_state and {case}")
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records
    if outcome == "skip":
        assert "requires_load" in records[0]["reason"]


@pytest.mark.parametrize("value", ["false", 1, [], {}])
def test_background_declaration_is_strict(tmp_path, value):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"] = {"background_responses": value}
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ProfileError, match="background_responses"):
        load(path)


@pytest.mark.parametrize("background", [False, True])
def test_undeclared_storage_sends_nothing(tmp_path, background):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"] = {"background_responses": background}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_state")
        assert not fleet.seen
    assert code == 0
    assert all(r["outcome"] == "skip" for r in records)
