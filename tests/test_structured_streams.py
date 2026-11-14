import pytest
import yaml

from tests.fakes import FakeFleet, message_events, named_sse
from tests.test_suites_against_fake import run_suites, write_profile


def profile_for(tmp_path, fleet, surface):
    path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["ingresses"][0]["surfaces"] = [surface]
    raw["capabilities"].update(
        structured_output=[surface], structured_output_streaming=[surface], json_object=[surface]
    )
    path.write_text(yaml.safe_dump(raw))
    return path


@pytest.mark.parametrize("surface", ["chat_completions", "messages", "responses"])
@pytest.mark.parametrize(
    "defect,passes", [(None, True), ("ignores_schema", False), ("invalid_json", False)]
)
def test_schema_streams(tmp_path, surface, defect, passes):
    with FakeFleet(structured_output_defect=defect) as fleet:
        path = profile_for(tmp_path, fleet, surface)
        code, records = run_suites(path, tmp_path / "run", "-k", "test_streamed_schema")
        assert len(fleet.seen) == 1
    assert code == (0 if passes else 1), records
    row = next(r for r in records if r["surface"] == surface)
    assert row["outcome"] == ("pass" if passes else "fail")
    assert any(
        c["name"].startswith("structured_output") and c["ok"] is passes for c in row["checks"]
    )


@pytest.mark.parametrize("surface", ["chat_completions", "responses"])
@pytest.mark.parametrize("defect,passes", [(None, True), ("ignored", False)])
def test_json_object_mode(tmp_path, surface, defect, passes):
    with FakeFleet(structured_output_defect=defect) as fleet:
        path = profile_for(tmp_path, fleet, surface)
        code, records = run_suites(path, tmp_path / "run", "-k", "test_json_object")
    assert code == (0 if passes else 1), records


def test_lifecycle_failure_is_not_hidden_by_output_validation(tmp_path):
    events = message_events()[:-1]
    with FakeFleet(messages_stream=named_sse(events)) as fleet:
        path = profile_for(tmp_path, fleet, "messages")
        code, records = run_suites(path, tmp_path / "run", "-k", "test_streamed_schema")
    assert code == 1, records
    row = next(r for r in records if r["surface"] == "messages")
    assert row["outcome"] == "fail"
    assert not any(c["name"].startswith("structured_output") for c in row["checks"])
