"""Responses streaming verdicts, including valid HTTP 200s with broken SSE content."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml

from llmdv.assertions.responses import (
    assert_response_contract,
    assert_response_stream_output,
    assert_responses_stream,
)
from llmdv.evidence import journal
from llmdv.http import redact, sse
from llmdv.http.client import RecordingClient
from llmdv.surfaces import get
from llmdv.surfaces.responses import TERMINAL_EVENTS
from tests.fakes import (
    FakeFleet,
    FakeResponsesConfig,
    named_sse,
    response_body,
    response_events,
    run_llmdv,
)
from tests.test_suites_against_fake import run_suites, write_profile

RESPONSES = get("responses")
CASE = "openai_responses.streaming_sse"


def transcript(events):
    return sse.read(named_sse(events).splitlines())


def completion(events):
    stream = transcript(events)
    assert_responses_stream(stream)
    result = RESPONSES.assemble([RESPONSES.parse_chunk(e.json()) for e in stream.data_events])
    assert_response_contract(result.raw)
    assert_response_stream_output(result, RESPONSES.parse_completion(result.raw))
    return result


def alter(kind, **fields):
    events = response_events()
    next(e for e in events if e["type"] == kind).update(fields)
    return events


def without(kind):
    return [e for e in response_events() if e["type"] != kind]


def snapshot(**fields):
    events = response_events()
    events[-1]["response"].update(fields)
    return events


def test_stream_assembles_text_and_uses_only_terminal_identity_status_and_usage():
    events = response_events()
    events[0]["response"]["usage"] = {
        "input_tokens": 999,
        "output_tokens": 999,
        "total_tokens": 1998,
    }
    result = completion(events)
    assert (result.role, result.object_type, result.model_echo) == (
        "assistant",
        "response",
        "fake/Model-1B",
    )
    assert result.text == "ok" and result.finish_reason == "stop"
    assert (
        result.usage.prompt_tokens,
        result.usage.completion_tokens,
        result.usage.total_tokens,
    ) == (3, 2, 5)
    assert result.raw == events[-1]["response"]


@pytest.mark.parametrize("reasoning", ["native", "summary", "both", "inline"])
def test_reasoning_is_assembled_without_duplicating_summaries_or_inline_tags(reasoning):
    body = response_body()
    if reasoning == "inline":
        body["output"][0]["content"][0]["text"] = "<think>考える</think>ok"
    else:
        item = {"id": "rs_fake", "type": "reasoning", "status": "completed", "summary": []}
        if reasoning in ("native", "both"):
            item["content"] = [{"type": "reasoning_text", "text": "考える"}]
        if reasoning in ("summary", "both"):
            item["summary"] = [{"type": "summary_text", "text": "考える"}]
        body["output"].insert(0, item)
    result = completion(response_events(response=body))
    assert (result.text, result.reasoning_text) == ("ok", "考える")


@pytest.mark.parametrize("terminal", ["response.completed", "response.incomplete"])
def test_reasoning_only_token_limited_streams_keep_native_finish_mapping(terminal):
    body = response_body()
    body.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    body["output"] = [
        {
            "id": "rs_fake",
            "type": "reasoning",
            "status": "incomplete",
            "content": [{"type": "reasoning_text", "text": "working"}],
            "summary": [],
        }
    ]
    events = response_events(response=body)
    events[-1]["type"] = terminal
    result = completion(events)
    assert result.finish_reason == "length"
    assert result.text == "" and result.reasoning_text == "working"


def test_standard_content_part_events_can_carry_reasoning():
    body = response_body()
    body["output"].insert(
        0,
        {
            "id": "rs_fake",
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "working"}],
        },
    )
    events = response_events(response=body)
    for event in events:
        event["type"] = event["type"].replace("reasoning_part", "content_part")
    assert completion(events).reasoning_text == "working"


def test_unknown_events_and_vendor_fields_do_not_become_output_or_metadata():
    events = response_events()
    for event in events:
        event["vendor"] = {"tokens": [1, 2]}
    events.insert(4, {"type": "response.vendor", "output_index": [], "delta": "not output"})
    assert completion(events).text == "ok"


def test_interleaved_parts_are_assembled_in_content_index_order():
    body = response_body()
    body["output"][0]["content"].append({"type": "output_text", "text": "!", "annotations": []})
    events = response_events(response=body)
    # Open part 1 between the two deltas of part 0, then deliver its delta there too.
    early = [
        e
        for e in events
        if e.get("content_index") == 1
        and e["type"] in ("response.content_part.added", "response.output_text.delta")
    ]
    events = [e for e in events if e not in early]
    events[5:5] = early
    for index, event in enumerate(events):
        event["sequence_number"] = index
    assert completion(events).text == "ok!"


@pytest.mark.parametrize("deltas", [2, 256])
def test_lifecycle_success_journal_stays_small(deltas):
    body = response_body()
    body["output"][0]["content"][0]["text"] = "x" * deltas
    active = journal.activate("responses-lifecycle")
    try:
        assert_responses_stream(transcript(response_events(response=body)))
        (check,) = active.checks
        assert check.name == "responses_lifecycle" and check.ok
        assert check.observed == {"events": deltas + 8, "items": 1}
        assert len(json.dumps(check.to_dict())) < 512
    finally:
        journal.deactivate()


INVALID = [
    pytest.param(without("response.created"), "responses_event_order", id="missing-created"),
    pytest.param(
        alter("response.in_progress", type="response.created"),
        "responses_event_order",
        id="duplicate-created",
    ),
    pytest.param(response_events()[:-1], "responses_terminal", id="missing-terminal"),
    pytest.param(without("response.output_item.added"), "responses_item_order", id="missing-item"),
    pytest.param(without("response.output_item.done"), "responses_item_order", id="unclosed-item"),
    pytest.param(without("response.content_part.added"), "responses_part_order", id="missing-part"),
    pytest.param(without("response.content_part.done"), "responses_part_order", id="unclosed-part"),
    pytest.param(
        without("response.output_text.done"), "responses_text_order", id="missing-text-done"
    ),
    pytest.param(
        alter("response.output_text.delta", item_id="different"),
        "responses_item_id",
        id="changed-item-id",
    ),
    pytest.param(
        alter("response.output_text.delta", content_index=1),
        "responses_part_order",
        id="wrong-part-index",
    ),
    pytest.param(
        alter("response.output_text.delta", delta=123), "responses_delta", id="non-string-delta"
    ),
    pytest.param(
        alter("response.output_text.delta", delta="lost"),
        "responses_text_consistency",
        id="changed-delta",
    ),
    pytest.param(
        without("response.output_text.delta"), "responses_text_consistency", id="missing-deltas"
    ),
    pytest.param(
        alter("response.output_text.delta", type="response.reasoning_text.delta"),
        "responses_text_order",
        id="wrong-delta-type",
    ),
    pytest.param(
        alter("response.output_text.delta", sequence_number=1),
        "responses_sequence",
        id="reordered-sequence",
    ),
    pytest.param(
        alter("response.output_text.delta", sequence_number=True),
        "responses_sequence",
        id="boolean-sequence",
    ),
    pytest.param(
        alter("response.output_text.delta", output_index=True),
        "responses_output_index",
        id="boolean-index",
    ),
    pytest.param(snapshot(id="different"), "responses_id", id="changed-response-id"),
    pytest.param(snapshot(model="different"), "responses_model", id="changed-model"),
    pytest.param(
        alter("response.completed", response=None), "responses_snapshot", id="missing-snapshot"
    ),
    pytest.param(
        alter("response.completed", type="response.incomplete"),
        "responses_terminal_status",
        id="wrong-incomplete-status",
    ),
    pytest.param(
        alter("response.completed", type="response.failed"),
        "responses_stream_error",
        id="failed-event",
    ),
    pytest.param(
        [*response_events()[:4], {"type": "error", "code": "server_error", "message": "aborted"}],
        "responses_stream_error",
        id="error-event",
    ),
]


@pytest.mark.parametrize(("events", "failed_check"), INVALID)
def test_broken_lifecycles_record_the_deciding_failure(events, failed_check):
    active = journal.activate("responses-defect")
    try:
        with pytest.raises(AssertionError, match=failed_check):
            assert_responses_stream(transcript(events))
        assert len(active.checks) == 1
        assert active.checks[0].name == failed_check and not active.checks[0].ok
    finally:
        journal.deactivate()


@pytest.mark.parametrize(
    "raw",
    [
        "event: response.created\ndata: {broken\n\n",
        "event: response.created\ndata: []\n\n",
        "event: response.created\ndata: {}\n\n",
        named_sse(response_events()).replace(
            "event: response.created", "event: response.in_progress", 1
        ),
        named_sse(response_events()).rstrip("\n"),
        "data: [DONE]\n\n",
    ],
)
def test_malformed_frames_and_unterminated_terminal_fail_with_evidence(tmp_path, raw):
    with FakeFleet(responses_stream=raw) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_responses_streaming")
    assert code == 1
    (record,) = records
    assert record["outcome"] == "fail" and record["http_status"] == 200
    assert any(not check["ok"] for check in record["checks"])
    assert (tmp_path / "run" / record["evidence"] / "transcript.sse").read_text() == raw + (
        "\n" if not raw.endswith("\n") else ""
    )


@pytest.mark.parametrize(("events", "failed_check"), INVALID[2:8] + INVALID[-6:])
def test_http_200_does_not_hide_stream_defects(tmp_path, events, failed_check):
    with FakeFleet(responses_stream=named_sse(events)) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_responses_streaming")
    assert code == 1
    (record,) = records
    assert record["outcome"] == "fail" and record["http_status"] == 200
    assert any(c["name"] == failed_check and not c["ok"] for c in record["checks"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"usage": None},
        {"usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 99}},
        {"status": "cancelled"},
        {"status": "in_progress"},
        {"error": {"code": "server_error", "message": "aborted"}},
    ],
)
def test_terminal_snapshot_keeps_existing_generation_checks(tmp_path, overrides):
    with FakeFleet(response_overrides=overrides) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_responses_streaming")
    assert code == 1
    (record,) = records
    assert record["outcome"] == "fail"
    assert any(not c["ok"] for c in record["checks"])


def test_a_full_terminal_snapshot_cannot_replace_streamed_output(tmp_path):
    events = response_events()
    # A proxy drops the entire output-item lifecycle but leaves the complete snapshot intact.
    raw = named_sse([events[0], events[1], events[-1]])
    with FakeFleet(responses_stream=raw) as fleet:
        code, records = run_suites(
            write_profile(tmp_path, fleet), tmp_path / "run", "-k", "test_responses_streaming"
        )
    assert code == 1
    assert records[0]["outcome"] == "fail"


def test_terminal_text_must_agree_with_streamed_output(tmp_path):
    events = response_events()
    events[-1]["response"]["output"][0]["content"][0]["text"] = "a different answer"
    with FakeFleet(responses_stream=named_sse(events)) as fleet:
        code, records = run_suites(
            write_profile(tmp_path, fleet), tmp_path / "run", "-k", "test_responses_streaming"
        )
    assert code == 1
    assert any(
        c["name"] == "responses_output_consistency" and not c["ok"] for c in records[0]["checks"]
    )


def test_item_done_cannot_change_its_streamed_text(tmp_path):
    events = response_events()
    item = next(e["item"] for e in events if e["type"] == "response.output_item.done")
    item["content"][0]["text"] = "changed"
    with FakeFleet(responses_stream=named_sse(events)) as fleet:
        code, records = run_suites(
            write_profile(tmp_path, fleet), tmp_path / "run", "-k", "test_responses_streaming"
        )
    assert code == 1
    assert any(c["name"] == "responses_item_content" and not c["ok"] for c in records[0]["checks"])


@pytest.mark.parametrize("defect", [None, "renamed_items"])
def test_snapshot_item_ids_must_match_the_streamed_items(tmp_path, defect):
    with FakeFleet(response_config=FakeResponsesConfig(stream_defect=defect)) as fleet:
        code, records = run_suites(
            write_profile(tmp_path, fleet), tmp_path / "run", "-k", "test_responses_streaming"
        )
    assert code == (1 if defect else 0), records
    failed = {c["name"] for c in records[0]["checks"] if not c["ok"]}
    assert failed == ({"responses_item_identity"} if defect else set())


def test_a_second_terminal_in_a_transcript_fails_lifecycle_validation():
    events = response_events()
    with pytest.raises(AssertionError, match="responses_event_order"):
        assert_responses_stream(transcript([*events, events[-1]]))


def test_cli_streams_per_ingress_and_preserves_named_event_evidence(tmp_path):
    with FakeFleet() as direct, FakeFleet() as router:
        profile = write_profile(tmp_path, direct)
        config = yaml.safe_load(profile.read_text())
        config["ingresses"].append(
            {**config["ingresses"][0], "id": "router", "base_url": router.base_url}
        )
        profile.write_text(yaml.safe_dump(config))
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify",
            "--profile",
            str(profile),
            "--surface",
            "responses",
            "--local-output",
            str(run_dir),
            "--pod-logs",
            "off",
            "--evidence",
            "all",
            "-k",
            "test_responses_streaming",
        )
    assert result.returncode == 0, result.stdout + result.stderr
    records = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    assert {r["ingress"] for r in records} == {"fake", "router"}
    for record in records:
        assert (record["case_id"], record["surface"], record["outcome"]) == (
            CASE,
            "responses",
            "pass",
        )
        assert all(check["ok"] for check in record["checks"])
        raw = (run_dir / record["evidence"] / "transcript.sse").read_text()
        assert "event: response.created" in raw and "event: response.completed" in raw
        assert "[DONE]" not in raw
    exchanges = [
        json.loads(line) for line in (run_dir / "exchanges.jsonl").read_text().splitlines()
    ]
    assert len(exchanges) == 2
    for exchange in exchanges:
        assert exchange["url"].endswith("/v1/responses")
        body = exchange["request_body"]
        assert body["store"] is False and body["stream"] is True
        assert "max_output_tokens" in body and "stream_options" not in body


@pytest.mark.parametrize("declared", [False, True])
def test_throttling_is_classified_without_retries(tmp_path, declared):
    with FakeFleet(throttle=True) as fleet:
        code, records = run_suites(
            write_profile(tmp_path, fleet, rate_limited=declared),
            tmp_path / "run",
            "-k",
            "test_responses_streaming",
        )
        assert len([request for request in fleet.seen if request[0] == "POST"]) == 1
    assert code == (0 if declared else 1)
    assert records[0]["outcome"] == ("skip" if declared else "fail")
    if declared:
        assert records[0]["skip_class"] == "throttled"


def test_undeclared_responses_skip_without_requests(tmp_path):
    with FakeFleet() as fleet:
        code, records = run_suites(
            write_profile(tmp_path, fleet, responses=False),
            tmp_path / "run",
            "-k",
            "test_responses_streaming",
        )
        assert not fleet.seen
    assert code == 0
    assert (records[0]["outcome"], records[0]["skip_class"]) == ("skip", "undeclared")


def test_non_sse_content_type_is_a_journaled_failure(tmp_path):
    with FakeFleet(responses_stream_content_type="application/json") as fleet:
        code, records = run_suites(
            write_profile(tmp_path, fleet), tmp_path / "run", "-k", "test_responses_streaming"
        )
    assert code == 1
    assert any(c["name"] == "stream_content_type" and not c["ok"] for c in records[0]["checks"])


@pytest.mark.parametrize("terminal", [*sorted(TERMINAL_EVENTS), "error"])
def test_transport_stops_at_every_terminal_without_reading_to_eof(terminal):
    raw = named_sse([{"type": terminal}])

    class HeldOpen(httpx.SyncByteStream):
        def __iter__(self):
            yield raw.encode()
            raise AssertionError("reader waited after the terminal event")

    def handle(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=HeldOpen())

    with RecordingClient("http://fake") as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(handle))
        exchange, stream = client.stream(
            "/v1/responses", json_body={}, stop_event=TERMINAL_EVENTS | {"error"}
        )
    assert exchange.transport_error is None
    assert len(stream.events) == 1 and exchange.response_text == raw


def test_error_diagnostics_redact_credentials():
    secret = "responses-stream-test-secret"
    redact.install_secrets([secret])
    active = journal.activate("responses-redaction")
    try:
        with pytest.raises(AssertionError) as caught:
            assert_responses_stream(transcript([{"type": "error", "message": secret}]))
        assert secret not in str(caught.value)
        assert secret not in json.dumps([check.to_dict() for check in active.checks])
    finally:
        journal.deactivate()
        redact.install_secrets([])


def test_streamed_tool_loop_checks_item_identity(tmp_path):
    with FakeFleet(
        response_config=FakeResponsesConfig(stream_defect="renamed_items"),
        response_tools=True,
    ) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["responses"]
        raw["model"]["tools"] = {"parser": "example"}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_responses_tool_stream")
    assert code == 1, records
    failed = {c["name"] for c in records[0]["checks"] if not c["ok"]}
    assert "responses_item_identity" in failed, failed
