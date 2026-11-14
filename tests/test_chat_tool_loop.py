import json

import pytest
import yaml

from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "prose_result",
        "argument_type",
        "schema",
        "json",
        "id",
        "name",
        "abort",
        "ignored_choice",
        "ignores_result",
    ],
)
def test_forced_call_and_result_round_trip(tmp_path, defect):
    with FakeFleet(chat_tools=True, chat_tool_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["chat_completions"]
        raw["model"]["tools"] = {"parser": "example-parser"}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_forced_tool_loop")
    assert code == (0 if defect in (None, "prose_result") else 1), records
    row = next(r for r in records if r["surface"] == "chat_completions")
    assert row["outcome"] == ("pass" if defect in (None, "prose_result") else "fail")
    exchanges = [
        json.loads(line) for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    assert len(exchanges) == (2 if defect in (None, "prose_result", "ignores_result") else 1)
    if defect in (None, "prose_result"):
        first, second = exchanges
        call = first["response_body"]["choices"][0]["message"]["tool_calls"][0]
        assert second["request_body"]["messages"][-1]["tool_call_id"] == call["id"]
        marker = first["request_body"]["tools"][0]["function"]["parameters"]["properties"][
            "marker"
        ]["enum"][0]
        assert marker not in first["request_body"]["messages"][0]["content"]


def test_tools_not_declared_sends_no_requests(tmp_path):
    with FakeFleet(chat_tools=True) as fleet:
        path = write_profile(tmp_path, fleet)
        code, records = run_suites(path, tmp_path / "run", "-k", "test_forced_tool_loop")
        assert not fleet.seen
    assert code == 0 and records[0]["outcome"] == "skip"


@pytest.mark.parametrize("case", ["test_forced_tool_loop", "test_streamed_tool_loop"])
@pytest.mark.parametrize(
    "declared,finish,defect,passes",
    [
        (None, "tool_calls", None, True),
        (None, "stop", None, False),
        ("tool_calls", "stop", None, False),
        ("stop", "stop", None, True),
        ("stop", "tool_calls", None, False),
        ("stop", "stop", "name", False),
        ("stop", "stop", "json", False),
    ],
)
def test_forced_finish_is_the_declared_one(tmp_path, case, declared, finish, defect, passes):
    """A forced named function must finish with the declared reason and still carry its call."""
    with FakeFleet(chat_tools=True, chat_tool_finish=finish, chat_tool_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["chat_completions"]
        raw["model"]["tools"] = {"parser": "example-parser"}
        if declared:
            raw["model"]["tools"]["forced_finish_reason"] = declared
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", case)
    assert code == (0 if passes else 1), records
    row = next(r for r in records if r["surface"] == "chat_completions")
    assert row["outcome"] == ("pass" if passes else "fail")
    check = next(c for c in row["checks"] if c["name"] == "tool_finish")
    assert (check["observed"], check["expected"]) == (finish, declared or "tool_calls")
    assert check["ok"] is (finish == check["expected"])
    if defect:
        # The declared finish matched, so the wrong call is what failed the case.
        assert any(c.get("ok") is False for c in row["checks"] if c["name"] != "tool_finish")
