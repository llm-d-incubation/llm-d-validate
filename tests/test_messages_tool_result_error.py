"""The is_error flag on a tool_result against a fleet that drops it."""

from __future__ import annotations

import pytest

from tests.test_messages_features import by_variant, failed, fake, run, sent

CASE = "anthropic_messages.tool_result_error_rendered"


@pytest.mark.parametrize("defect,passes", [(None, True), ("drops_is_error", False)])
def test_the_error_flag_must_change_the_prompt(tmp_path, defect, passes):
    with fake(defect=defect) as fleet:
        code, records = run(
            tmp_path, fleet, "test_tool_result_error_flag", checks=["tool_result_error"]
        )
    (record,) = by_variant(records, CASE).values()
    assert code == (0 if passes else 1)
    assert record["outcome"] == ("pass" if passes else "fail"), record
    assert failed(record, "tool_result_error_rendered") is not passes
    flags = [e["request_body"]["messages"][-1]["content"][0]["is_error"] for e in sent(tmp_path)]
    assert flags == [False, True]


def test_undeclared_sends_nothing(tmp_path):
    with fake() as fleet:
        code, records = run(tmp_path, fleet, "test_tool_result_error_flag")
        assert not [s for s in fleet.seen if s[0] == "POST"]
    assert code == 0 and {r["skip_class"] for r in records} == {"undeclared"}
