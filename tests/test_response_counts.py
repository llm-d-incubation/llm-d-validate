import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from tests.fakes import (
    FakeFleet,
    FakeResponsesConfig,
    named_sse,
    reasoning_only_response,
    response_events,
)
from tests.test_suites_against_fake import run_suites, write_profile


def run(tmp_path, selector, capabilities=None, **flags):
    with FakeFleet(**flags) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        if capabilities:
            raw = yaml.safe_load(path.read_text())
            raw["capabilities"].update(capabilities)
            path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", selector)
    return code, records


@pytest.mark.parametrize("defect,outcome", [(None, "pass"), ("no_completed_at", "fail")])
def test_completed_at(tmp_path, defect, outcome):
    code, records = run(
        tmp_path,
        "test_completed_response_has_a_completion_time",
        response_config=FakeResponsesConfig(shape_defect=defect),
    )
    (record,) = records
    assert record["outcome"] == outcome, record
    assert code == (1 if outcome == "fail" else 0)


@pytest.mark.parametrize(
    "declared,flags,outcome",
    [
        (None, {}, "pass"),
        (None, {"response_config": FakeResponsesConfig(serve_input_tokens=True)}, "pass"),
        (
            None,
            {"response_config": FakeResponsesConfig(input_tokens_defect="server_error")},
            "fail",
        ),
        (
            None,
            {"response_config": FakeResponsesConfig(input_tokens_defect="constant")},
            "fail",
        ),
        (True, {"response_config": FakeResponsesConfig(serve_input_tokens=True)}, "pass"),
        (True, {}, "fail"),
        (
            True,
            {"response_config": FakeResponsesConfig(input_tokens_defect="constant")},
            "fail",
        ),
        (False, {}, "pass"),
        (False, {"response_config": FakeResponsesConfig(serve_input_tokens=True)}, "fail"),
    ],
)
def test_input_tokens(tmp_path, declared, flags, outcome):
    capabilities = None if declared is None else {"responses_input_tokens": declared}
    code, records = run(tmp_path, "test_input_token_count", capabilities, **flags)
    (record,) = records
    assert record["outcome"] == outcome, record
    assert code == (1 if outcome == "fail" else 0)


@pytest.mark.parametrize("name", ["responses_input_tokens", "responses_compact"])
def test_endpoint_declarations_are_booleans(tmp_path, name):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"capabilities": {name: "on"}}))
    with pytest.raises(ProfileError, match=name):
        load(path)


@pytest.mark.parametrize(
    "flags,outcome",
    [
        ({}, "pass"),
        ({"response_overrides": {"status": "failed"}}, "fail"),
        ({"responses_stream": named_sse(response_events()[:-1])}, "fail"),
    ],
)
def test_sdk_parse(tmp_path, flags, outcome):
    code, records = run(tmp_path, "test_responses_parse_with_pinned_openai_sdk", **flags)
    outcomes = {r["nodeid"].rstrip("]").rsplit("-", 1)[1]: r["outcome"] for r in records}
    if "responses_stream" in flags:
        assert outcomes == {"json": "pass", "sse": "fail"}, records
    else:
        assert outcomes == {"json": outcome, "sse": outcome}, records


def test_sdk_stream_stops_at_the_terminal_event(tmp_path):
    trailing_invalid_event = "event: response.output_text.delta\ndata: not-json\n\n"
    stream = named_sse(response_events()) + trailing_invalid_event
    code, records = run(
        tmp_path,
        "test_responses_parse_with_pinned_openai_sdk",
        responses_stream=stream,
    )
    outcomes = {r["nodeid"].rstrip("]").rsplit("-", 1)[1]: r["outcome"] for r in records}
    assert outcomes == {"json": "pass", "sse": "pass"}, records
    assert code == 0


@pytest.mark.parametrize(
    "declared,flags,outcome",
    [
        (None, {}, "pass"),
        (None, {"response_config": FakeResponsesConfig(serve_compact=True)}, "pass"),
        (
            None,
            {"response_config": FakeResponsesConfig(compact_defect="server_error")},
            "fail",
        ),
        (None, {"response_config": FakeResponsesConfig(compact_defect="no_item")}, "fail"),
        (True, {"response_config": FakeResponsesConfig(serve_compact=True)}, "pass"),
        (True, {}, "fail"),
        (False, {}, "pass"),
        (False, {"response_config": FakeResponsesConfig(serve_compact=True)}, "fail"),
    ],
)
def test_compact(tmp_path, declared, flags, outcome):
    capabilities = None if declared is None else {"responses_compact": declared}
    code, records = run(tmp_path, "test_compact_endpoint", capabilities, **flags)
    (record,) = records
    assert record["outcome"] == outcome, record
    assert code == (1 if outcome == "fail" else 0)


@pytest.mark.parametrize(
    "declared,flags,outcome",
    [
        (None, {"response_config": FakeResponsesConfig(serve_compact=True)}, "skip"),
        (True, {"response_config": FakeResponsesConfig(serve_compact=True)}, "pass"),
        (
            True,
            {
                "response_config": FakeResponsesConfig(serve_compact=True),
                "response_overrides": reasoning_only_response(),
            },
            "fail",
        ),
        (
            True,
            {
                "response_config": FakeResponsesConfig(
                    serve_compact=True,
                    input_defect="rejects_compaction",
                )
            },
            "fail",
        ),
    ],
)
def test_compact_replay(tmp_path, declared, flags, outcome):
    capabilities = None if declared is None else {"responses_compact": declared}
    code, records = run(tmp_path, "test_compacted_history_replays", capabilities, **flags)
    (record,) = records
    assert record["outcome"] == outcome, record
    if "response_overrides" in flags:
        assert any(
            check["name"] == "answer_budget" and not check["ok"] for check in record["checks"]
        )
    assert code == (1 if outcome == "fail" else 0)
