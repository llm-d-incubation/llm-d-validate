"""The IDE agent session case, proven to fail on each defect it targets."""

import pytest
import yaml

from tests.fakes import FakeFleet
from tests.test_cache_repeats import TOOLS
from tests.test_cache_reuse import BLOCKS, RATIO, run_cache_case
from tests.test_suites_against_fake import run_suites, write_profile

IDE = {"chat_reasoning_efforts": ["low", "high"]}


@pytest.mark.parametrize("model", [None, TOOLS])
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (BLOCKS, None, "pass"),
        (RATIO, None, "pass"),
        (BLOCKS, "history_blind", "fail"),
        (RATIO, "history_blind", "fail"),
    ],
)
def test_ide_session_reads_the_first_turn(tmp_path, model, declaration, defect, outcome):
    code, records = run_cache_case(
        tmp_path, "chat_completions", declaration, "test_ide_session", defect, model=model, **IDE
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


def run_chat(tmp_path, select, **fleet_options):
    with FakeFleet(cache_probe=True, cache_block=16, max_model_len=4096, **fleet_options) as fleet:
        path = write_profile(tmp_path, fleet, responses=True, max_model_len=4096)
        raw = yaml.safe_load(path.read_text())
        raw["model"].update(TOOLS)
        raw["ingresses"][0]["surfaces"] = ["chat_completions"]
        cache = {"surfaces": ["chat_completions"], "prefix_words": 128, **BLOCKS}
        raw["capabilities"] = {"cache_reuse": cache, **IDE}
        path.write_text(yaml.safe_dump(raw))
        return run_suites(
            path, tmp_path / "run", "-k", select, "--llmdv-surface", "chat_completions"
        )


@pytest.mark.parametrize(
    "select,outcome", [("test_ide_session", "fail"), ("test_turn_reuse", "pass")]
)
def test_only_the_ide_session_sends_max_completion_tokens(tmp_path, select, outcome):
    code, records = run_chat(tmp_path, select, refuse_max_completion_tokens=True)
    assert {r["outcome"] for r in records} == {outcome}, records
    assert code == (1 if outcome == "fail" else 0), records


def test_ide_session_sends_the_agent_parts(tmp_path):
    code, records = run_cache_case(
        tmp_path, "chat_completions", BLOCKS, "test_ide_session", model=TOOLS, **IDE
    )
    assert code == 0, records
    parts = next(c["observed"] for c in records[0]["checks"] if c["name"] == "ide_parts")
    assert parts == {
        "max_completion_tokens": 2048,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning_effort": "high",
        "tools": 2,
        "cache_hints": False,
    }, parts
