"""The Responses CLI session case, proven to fail on each defect it targets."""

import pytest

from tests.test_cache_repeats import TOOLS
from tests.test_cache_reuse import BLOCKS, RATIO, run_cache_case

CLI = {"responses_reasoning_efforts": ["low", "high"]}
HINTED = {**CLI, "scenario_checks": ["cache_hints"]}


def run_cli(tmp_path, declaration, defect=None, model=TOOLS, select="test_cli_session", **caps):
    return run_cache_case(
        tmp_path, "responses", declaration, select, defect, model=model, **(caps or CLI)
    )


@pytest.mark.parametrize("model", [None, TOOLS])
@pytest.mark.parametrize(
    "declaration,defect,capabilities,outcome",
    [
        (BLOCKS, None, CLI, "pass"),
        (RATIO, None, CLI, "pass"),
        (BLOCKS, None, HINTED, "pass"),
        # The CLI's block-form input is placed blind; the raw session sends strings.
        (BLOCKS, "input_blocks_blind", CLI, "fail"),
        (BLOCKS, "history_blind", CLI, "fail"),
        (BLOCKS, "input_blocks_blind", HINTED, "fail"),
    ],
)
def test_cli_session_fails_on_each_defect(
    tmp_path, model, declaration, defect, capabilities, outcome
):
    code, records = run_cli(tmp_path, declaration, defect, model, **capabilities)
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


def test_raw_responses_session_misses_blind_input_blocks(tmp_path):
    code, records = run_cli(tmp_path, BLOCKS, "input_blocks_blind", select="test_turn_reuse")
    assert code == 0 and {r["outcome"] for r in records} == {"pass"}, records


def test_cli_session_sends_the_cli_parts(tmp_path):
    code, records = run_cli(tmp_path, BLOCKS, **HINTED)
    assert code == 0, records
    parts = next(c["observed"] for c in records[0]["checks"] if c["name"] == "cli_parts")
    assert parts == {
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "tools": 2,
        "reasoning": {"effort": "high", "summary": "auto"},
        "max_output_tokens": 2048,
        "cache_hints": True,
    }, parts


@pytest.mark.parametrize(
    "surface,select,capabilities",
    [
        ("responses", "test_cli_session", CLI),
        ("chat_completions", "test_ide_session", {"chat_reasoning_efforts": ["high"]}),
        ("messages", "test_composed_session", {}),
    ],
)
def test_agent_sessions_skip_when_the_declared_window_cannot_fit_them(
    tmp_path, surface, select, capabilities
):
    code, records = run_cache_case(
        tmp_path,
        surface,
        BLOCKS,
        select,
        model={"max_model_len": 2048},
        **capabilities,
    )
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "[undeclared]" in records[0]["reason"], records
    assert "2048-token window" in records[0]["reason"], records
    exchanges = tmp_path / "run" / "exchanges.jsonl"
    assert not exchanges.exists() or not exchanges.read_text().strip()


@pytest.mark.parametrize(
    "surface,select,capabilities",
    [
        ("responses", "test_cli_session", CLI),
        ("chat_completions", "test_ide_session", {"chat_reasoning_efforts": ["high"]}),
        ("messages", "test_composed_session", {}),
    ],
)
def test_agent_sessions_skip_when_the_prefix_fills_the_window(
    tmp_path, surface, select, capabilities
):
    code, records = run_cache_case(
        tmp_path,
        surface,
        {**BLOCKS, "prefix_words": 4096},
        select,
        model={"max_model_len": 4096},
        **capabilities,
    )
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "[undeclared]" in records[0]["reason"], records
