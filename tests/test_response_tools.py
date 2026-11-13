import json

import pytest
import yaml

from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


def run_tools(tmp_path, defect, stream):
    with FakeFleet(response_tools=True, response_tool_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["responses"]
        raw["model"]["tools"] = {"parser": "example"}
        path.write_text(yaml.safe_dump(raw))
        selected = "test_responses_tool_stream" if stream else "test_forced_tool_loop"
        return run_suites(path, tmp_path / "run", "-k", selected)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "defect",
    [
        None,
        "prose_result",
        "schema",
        "argument_type",
        "json",
        "id",
        "name",
        "abort",
        "ignores_result",
    ],
)
def test_responses_tool_loop(tmp_path, stream, defect):
    code, records = run_tools(tmp_path, defect, stream)
    assert code == (0 if defect in (None, "prose_result") else 1), records
    row = next(r for r in records if r["surface"] == "responses")
    assert row["outcome"] == ("pass" if defect in (None, "prose_result") else "fail"), row
    if defect in (None, "prose_result"):
        exchanges = [
            json.loads(line)
            for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
        ]
        result = exchanges[1]["request_body"]["input"][-1]
        assert result["type"] == "function_call_output"
        assert result["call_id"] == "call_fake"
        assert exchanges[1]["request_body"]["input"][-2]["id"] == "fc_fake"


@pytest.mark.parametrize(
    "defect", ["dropped_fragment", "changed_call_id", "stale_item", "stale_terminal"]
)
def test_function_stream_does_not_trust_snapshots(tmp_path, defect):
    code, records = run_tools(tmp_path, defect, True)
    assert code == 1, records
    assert records[0]["outcome"] == "fail", records
