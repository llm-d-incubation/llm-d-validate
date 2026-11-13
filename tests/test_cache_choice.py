"""The tool-choice reuse case, proven to fail on a render that drops tools for `none`."""

import json

import pytest

from tests.test_cache_repeats import SURFACES, TOOLS
from tests.test_cache_reuse import BLOCKS, RATIO, run_cache_case

SELECT = "test_tool_choice_reuse"


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (BLOCKS, None, "pass"),
        (RATIO, None, "pass"),
        (BLOCKS, "tools_dropped_on_none", "fail"),
        (RATIO, "tools_dropped_on_none", "fail"),
    ],
)
def test_tool_choice_none_keeps_reuse(tmp_path, surface, declaration, defect, outcome):
    code, records = run_cache_case(tmp_path, surface, declaration, SELECT, defect, model=TOOLS)
    assert code == (1 if outcome == "fail" else 0), records
    assert [r["outcome"] for r in records] == [outcome], records


@pytest.mark.parametrize(
    "surface,choice",
    [("chat_completions", "none"), ("responses", "none"), ("messages", {"type": "none"})],
)
def test_tool_choice_none_in_each_surface_form(tmp_path, surface, choice):
    code, _ = run_cache_case(tmp_path, surface, BLOCKS, SELECT, model=TOOLS)
    assert code == 0
    bodies = [
        json.loads(line)["request_body"]
        for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    assert [len(body["tools"]) for body in bodies] == [2, 2]
    assert "tool_choice" not in bodies[0] and bodies[1]["tool_choice"] == choice


def test_tool_choice_reuse_skips_without_automatic_choice(tmp_path):
    code, records = run_cache_case(tmp_path, "chat_completions", BLOCKS, SELECT)
    assert code == 0, records
    assert records[0]["outcome"] == "skip" and "[undeclared]" in records[0]["reason"]


def test_tools_order_case_misses_a_render_that_drops_tools_for_none(tmp_path):
    """The tool order cases never change the choice, so the defect never shows there."""
    code, records = run_cache_case(
        tmp_path,
        "chat_completions",
        BLOCKS,
        "test_tools_cache_reuse",
        "tools_dropped_on_none",
        model=TOOLS,
    )
    assert code == 0, records
    assert {r["outcome"] for r in records} == {"pass"}, records
