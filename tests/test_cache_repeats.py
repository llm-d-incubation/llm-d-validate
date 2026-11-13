"""Cache repeat cases beyond the plain prefix, each proven to fail on the defect it targets."""

import pytest

from tests.test_cache_reuse import BLOCKS, RATIO, run_cache_case

SURFACES = ["chat_completions", "messages", "responses"]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (RATIO, None, "pass"),
        (BLOCKS, None, "pass"),
        # Counters missing from the Chat usage frame or the Responses snapshot, and Messages
        # counters reported only in the final delta while message_start shows a cold prompt.
        (BLOCKS, "stream_cache_missing", "fail"),
        (RATIO, "stream_cache_missing", "fail"),
        # Messages counters only in the final delta, with message_start silent on the cache:
        # an SDK reads them there, so the reuse is reported (#333).
        (BLOCKS, "stream_cache_in_delta", "pass"),
        (RATIO, "stream_cache_in_delta", "pass"),
    ],
)
def test_streamed_reuse_reads_where_clients_read(tmp_path, surface, declaration, defect, outcome):
    code, records = run_cache_case(
        tmp_path, surface, declaration, "test_streamed_cache_reuse", defect
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("defect,outcome", [(None, "pass"), ("tiny_output_uncached", "fail")])
def test_single_token_reuse_tolerates_no_answer(tmp_path, surface, defect, outcome):
    code, records = run_cache_case(
        tmp_path, surface, BLOCKS, "test_single_token_cache_reuse", defect
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records
    if defect is None:
        checks = {c["name"]: c["observed"] for c in records[0]["checks"]}
        assert checks["output_chars"].startswith("content=0"), checks


TOOLS = {"tools": {"parser": "fake", "auto_choice": True}}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("defect,outcome", [(None, "pass"), ("tools_unstable", "fail")])
def test_tools_reuse_needs_a_stable_tool_render(tmp_path, surface, defect, outcome):
    code, records = run_cache_case(
        tmp_path, surface, BLOCKS, "test_tools_cache_reuse", defect, model=TOOLS
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("defect,outcome", [(None, "pass"), ("key_order_sensitive", "fail")])
def test_tool_key_order_needs_a_canonical_render(tmp_path, surface, defect, outcome):
    declaration = {**BLOCKS, "canonical_tool_keys": True}
    code, records = run_cache_case(
        tmp_path, surface, declaration, "test_tool_key_order_cache_reuse", defect, model=TOOLS
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("select", ["test_tools_cache_reuse", "test_tool_key_order_cache_reuse"])
def test_tool_cases_start_cold_on_a_cache_an_earlier_run_filled(tmp_path, surface, select):
    """Tools render first, so the probe leads each description: the second night shares no
    4-token block with the first, and its first request is still cold."""
    declaration = {**BLOCKS, "block_size": 4, "canonical_tool_keys": True}
    code, records = run_cache_case(tmp_path, surface, declaration, select, model=TOOLS, nights=2)
    assert code == 0 and records[0]["outcome"] == "pass", records


TOOL_CASES = [
    "test_tools_cache_reuse",
    "test_tool_key_order_cache_reuse",
    "test_large_toolset_cache_reuse",
]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("select", TOOL_CASES)
@pytest.mark.parametrize("defect,outcome", [(None, "pass"), ("stale_first", "fail")])
def test_tool_cases_start_cold_behind_a_fixed_tools_section(
    tmp_path, surface, select, defect, outcome
):
    """A template with fixed tool instructions ahead of the schemas renders three whole blocks
    every tools request shares. The second night's first request must still read nothing, as
    the probe-led system prompt comes first; a first request that does read is still a failure.
    """
    declaration = {**BLOCKS, "canonical_tool_keys": True}
    code, records = run_cache_case(
        tmp_path,
        surface,
        declaration,
        select,
        defect,
        model=TOOLS,
        nights=2,
        fake={"tool_preamble": 3 * BLOCKS["block_size"]},
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records
    if outcome == "fail":
        assert "first read" in records[0]["reason"], records
        assert any(c["name"] == "cache_cold" and not c["ok"] for c in records[0]["checks"])


@pytest.mark.parametrize(
    "select,model",
    [
        ("test_tools_cache_reuse", {"tools": {"parser": "fake"}}),
        ("test_tool_key_order_cache_reuse", TOOLS),
    ],
)
def test_tool_cases_skip_without_their_declarations(tmp_path, select, model):
    code, records = run_cache_case(tmp_path, "chat_completions", BLOCKS, select, model=model)
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "[undeclared]" in records[0]["reason"], records


@pytest.mark.parametrize("defect,outcome", [(None, "pass"), ("reasoning_blind", "fail")])
def test_each_reasoning_effort_reuses_its_own_prefix(tmp_path, defect, outcome):
    code, records = run_cache_case(
        tmp_path,
        "chat_completions",
        BLOCKS,
        "test_reasoning_effort_cache_reuse",
        defect,
        chat_reasoning_efforts=["low", "high"],
    )
    ran = {r["variant"]: r["outcome"] for r in records if r["outcome"] != "skip"}
    assert code == (1 if outcome == "fail" else 0), records
    assert sorted(ran) == ["high", "low"], records
    assert set(ran.values()) == {outcome}, records


@pytest.mark.parametrize("defect,outcome", [(None, "pass"), ("reasoning_blind", "fail")])
def test_each_thinking_mode_reuses_its_own_prefix(tmp_path, defect, outcome):
    code, records = run_cache_case(
        tmp_path,
        "messages",
        BLOCKS,
        "test_thinking_mode_cache_reuse",
        defect,
        messages_thinking=["disabled"],
    )
    ran = {r["variant"]: r["outcome"] for r in records if r["outcome"] != "skip"}
    assert code == (1 if outcome == "fail" else 0), records
    assert ran == {"disabled": outcome}, records
