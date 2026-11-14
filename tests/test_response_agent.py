import pytest
import yaml

from tests.fakes import FakeFleet, FakeResponsesConfig
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize(
    "flags,outcome",
    [
        ({}, "pass"),
        ({"response_config": FakeResponsesConfig(feature_mode="refuse")}, "fail"),
        ({"response_config": FakeResponsesConfig(feature_mode="server_error")}, "fail"),
        # Item identity is checked by the stream contract cases only.
        ({"response_config": FakeResponsesConfig(stream_defect="renamed_items")}, "pass"),
        ({"answer_leak": "untagged"}, "fail"),
    ],
)
def test_coding_agent_request(tmp_path, flags, outcome):
    with FakeFleet(**flags) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["model"]["tools"] = {"parser": "example"}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_coding_agent_request")
    (record,) = records
    assert record["outcome"] == outcome, record
    assert code == (1 if outcome == "fail" else 0)


def test_coding_agent_request_needs_tools(tmp_path):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        code, records = run_suites(path, tmp_path / "run", "-k", "test_coding_agent_request")
        assert not fleet.seen
    (record,) = records
    assert record["outcome"] == "skip" and "undeclared" in record["reason"]
