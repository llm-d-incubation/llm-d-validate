import json

import pytest
import yaml

from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "defect",
    [None, "prose_result", "schema", "argument_type", "id", "name", "abort", "ignores_result"],
)
def test_messages_tool_loop(tmp_path, stream, defect):
    with FakeFleet(message_tools=True, message_tool_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["messages"]
        raw["model"]["tools"] = {"parser": "example"}
        path.write_text(yaml.safe_dump(raw))
        selected = "test_messages_tool_stream" if stream else "test_forced_tool_loop"
        code, records = run_suites(path, tmp_path / "run", "-k", selected)
    assert code == (0 if defect in (None, "prose_result") else 1), records
    row = next(r for r in records if r["surface"] == "messages")
    assert row["outcome"] == ("pass" if defect in (None, "prose_result") else "fail")
    if defect in (None, "prose_result"):
        exchanges = [
            json.loads(line)
            for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
        ]
        assistant = exchanges[1]["request_body"]["messages"][-2]["content"]
        assert assistant[0]["signature"] == "sig-fake"
        assert assistant[2]["type"] == "tool_use"
        assert isinstance(assistant[2]["input"], dict)


@pytest.mark.parametrize("defect", ["dropped_fragment", "index", "nonempty_start"])
def test_broken_tool_deltas_fail(tmp_path, defect):
    with FakeFleet(message_tools=True, message_tool_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["messages"]
        raw["model"]["tools"] = {"parser": "example"}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_messages_tool_stream")
    assert code == 1, records
    assert records[0]["outcome"] == "fail"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("declared", [None, "stop"])
def test_forced_tool_use_must_end_with_tool_use(tmp_path, stream, declared):
    """Messages has one finish for a forced tool; a Chat declaration does not relax it."""
    with FakeFleet(message_tools=True, message_tool_defect="end_turn") as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["messages"]
        raw["model"]["tools"] = {"parser": "example"}
        if declared:
            raw["model"]["tools"]["forced_finish_reason"] = declared
        path.write_text(yaml.safe_dump(raw))
        selected = "test_messages_tool_stream" if stream else "test_forced_tool_loop"
        code, records = run_suites(path, tmp_path / "run", "-k", selected)
    assert code == 1, records
    row = next(r for r in records if r["surface"] == "messages")
    assert row["outcome"] == "fail"
    check = next(c for c in row["checks"] if c["name"] == "tool_finish")
    assert (check["observed"], check["expected"], check["ok"]) == ("end_turn", "tool_use", False)
