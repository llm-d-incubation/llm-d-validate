"""The SDK session case, proven to fail on each defect it targets."""

import pytest
import yaml

from tests.fakes import FakeFleet
from tests.test_cache_repeats import SURFACES
from tests.test_cache_reuse import BLOCKS, RATIO, run_cache_case
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (BLOCKS, None, "pass"),
        (RATIO, None, "pass"),
        (BLOCKS, "history_blind", "fail"),
        (RATIO, "history_blind", "fail"),
        # No cache counters in usage, so the SDK's typed field is empty.
        (BLOCKS, "missing", "fail"),
        # A cached count larger than the prompt is impossible.
        (RATIO, "impossible", "fail"),
    ],
)
def test_sdk_session_reads_each_turn(tmp_path, surface, declaration, defect, outcome):
    code, records = run_cache_case(tmp_path, surface, declaration, "test_sdk_session_reuse", defect)
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


def run_chat(tmp_path, select, **fleet_options):
    with FakeFleet(cache_probe=True, cache_block=16, **fleet_options) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["chat_completions"]
        cache = {"surfaces": ["chat_completions"], "prefix_words": 128, **BLOCKS}
        raw["capabilities"] = {"cache_reuse": cache}
        path.write_text(yaml.safe_dump(raw))
        return run_suites(
            path, tmp_path / "run", "-k", select, "--llmdv-surface", "chat_completions"
        )


def test_sdk_session_fails_where_passed_back_nulls_are_refused(tmp_path):
    code, records = run_chat(tmp_path, "test_sdk_session_reuse", refuse_null_fields=True)
    assert code == 1 and records[0]["outcome"] == "fail", records
    assert "400" in records[0]["reason"], records


@pytest.mark.parametrize("select", ["test_native_cache_reuse", "test_turn_reuse"])
def test_raw_cases_miss_refused_nulls(tmp_path, select):
    code, records = run_chat(tmp_path, select, refuse_null_fields=True)
    assert code == 0 and {r["outcome"] for r in records} == {"pass"}, records


STREAM = "test_sdk_stream_session_reuse"


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "defect,outcome",
    [
        (None, "pass"),
        ("history_blind", "fail"),
        # The streamed usage carries no cache counter where the stream helper reads it. On
        # Messages the helper adds the final delta's read to a start that reported the prompt
        # cold, so the prompt it reports is too large for the next turn's floor.
        ("stream_cache_missing", "fail"),
        # The SDK copies a final delta's input and cache counts into the final message.
        ("stream_cache_in_delta", "pass"),
        ("impossible", "fail"),
    ],
)
def test_sdk_stream_session_reads_each_turn(tmp_path, surface, defect, outcome):
    code, records = run_cache_case(tmp_path, surface, BLOCKS, STREAM, defect)
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


def test_sdk_stream_session_skips_with_a_replica_header(tmp_path):
    code, records = run_cache_case(tmp_path, "chat_completions", RATIO, STREAM)
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "[not_implemented]" in records[0]["reason"], records


def test_sdk_stream_session_fails_where_passed_back_nulls_are_refused(tmp_path):
    code, records = run_chat(tmp_path, STREAM, refuse_null_fields=True)
    assert code == 1 and records[0]["outcome"] == "fail", records
    assert "400" in records[0]["reason"], records


@pytest.mark.parametrize(
    "defect,terminal,outcome,check",
    [
        # Every reply reasons to max_output_tokens and ends incomplete, as the API defines: the
        # helper's get_final_response() raises on it, the terminal response is a length finish.
        ("budget_spent", "response.incomplete", "pass", None),
        ("generation_failed", "response.failed", "fail", "finish_reason_recognised"),
        ("no_terminal", None, "fail", "sdk_stream_terminal"),
    ],
)
def test_sdk_stream_session_reads_the_responses_terminal_event(
    tmp_path, defect, terminal, outcome, check
):
    code, records = run_cache_case(tmp_path, "responses", BLOCKS, STREAM, defect)
    (record,) = records
    assert code == (1 if outcome == "fail" else 0), record
    assert record["outcome"] == outcome, record
    terminals = [c["observed"] for c in record["checks"] if c["name"] == "sdk_stream_terminal"]
    assert terminals and terminals[0] == terminal, record["checks"]
    if check:
        assert any(c["name"] == check and not c["ok"] for c in record["checks"]), record
