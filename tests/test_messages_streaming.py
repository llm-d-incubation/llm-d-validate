"""Exercise the Messages lifecycle and its verdicts using real named SSE frames."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml

from llmdv.assertions.messages import assert_messages_stream
from llmdv.evidence import journal
from llmdv.http import redact, sse
from llmdv.http.client import RecordingClient
from llmdv.surfaces import get
from tests.fakes import FakeFleet, message_events, named_sse, run_llmdv
from tests.test_suites_against_fake import run_suites, write_profile

MESSAGES = get("messages")
CASE = "anthropic_messages.streaming_sse"


def transcript(events):
    return sse.read(named_sse(events).splitlines())


def completion(events):
    stream = transcript(events)
    assert_messages_stream(stream)
    return MESSAGES.assemble([MESSAGES.parse_chunk(e.json()) for e in stream.events])


def alter(event_type, **fields):
    events = message_events()
    next(e for e in events if e["type"] == event_type).update(fields)
    return events


def start_fields(**fields):
    events = message_events()
    events[0]["message"].update(fields)
    return events


def test_text_stream_retains_identity_and_native_usage():
    result = completion(message_events())
    assert (result.role, result.object_type, result.model_echo) == (
        "assistant",
        "message",
        "fake/Model-1B",
    )
    assert result.text == "ok"
    assert result.finish_reason == "end_turn"
    assert result.usage.prompt_tokens == 3
    assert result.usage.completion_tokens == 2
    assert result.usage.total_tokens is None


def test_native_thinking_and_signatures_precede_the_text_block():
    events = message_events()
    for event in events:
        if "index" in event:
            event["index"] += 1
    events[1:1] = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "考"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "える"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "opaque-signature"},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    result = completion(events)
    assert (result.text, result.reasoning_text) == ("ok", "考える")
    assert result.raw["events"] == events


def test_inline_thinking_is_split_only_after_fragment_assembly():
    events = message_events()
    events[3]["delta"]["text"] = "<thi"
    events[4]["delta"]["text"] = "nk>working</think>ok"
    result = completion(events)
    assert (result.text, result.reasoning_text) == ("ok", "working")


@pytest.mark.parametrize(
    "event_type", ["vendor_event", "ping", "started", "__init__", "_block_index"]
)
def test_cumulative_usage_is_not_summed_and_extensions_are_tolerated(event_type):
    events = message_events()
    events.insert(
        -1, {"type": "message_delta", "delta": {}, "usage": {"input_tokens": 4, "output_tokens": 5}}
    )
    events.insert(
        2,
        {
            "type": event_type,
            "delta": {"type": "text_delta", "text": "ignore"},
            "usage": {"input_tokens": 100},
        },
    )
    result = completion(events)
    assert result.text == "ok"
    assert result.usage.completion_tokens == 5
    assert result.usage.prompt_tokens == 4
    assert result.finish_reason == "end_turn"


def test_start_usage_never_substitutes_for_missing_final_output_count():
    events = alter("message_delta", usage={})
    chunks = [MESSAGES.parse_chunk(e) for e in events]
    assert chunks[0].usage.prompt_tokens == 3
    assert chunks[0].usage.completion_tokens is None
    result = MESSAGES.assemble(chunks)
    assert result.usage.prompt_tokens == 3
    assert result.usage.completion_tokens is None


@pytest.mark.parametrize("deltas", [2, 256])
def test_success_records_one_small_lifecycle_summary_regardless_of_stream_length(deltas):
    events = message_events()
    events[3:5] = [events[3]] * deltas
    events[-2]["usage"]["output_tokens"] = deltas
    checks = journal.activate("messages-summary")
    try:
        assert_messages_stream(transcript(events))
        (check,) = checks.checks
        assert check.name == "messages_lifecycle"
        assert check.ok
        assert check.observed == {"events": deltas + 6, "blocks": 1, "output_tokens": deltas}
        assert len(json.dumps([check.to_dict()]).encode("utf-8")) < 512
    finally:
        journal.deactivate()


INVALID = [
    pytest.param(message_events()[1:], "messages_event_order", id="missing-start"),
    pytest.param(
        [message_events()[0], *message_events()], "messages_event_order", id="duplicate-start"
    ),
    pytest.param(start_fields(type=None), "messages_start_type", id="missing-type"),
    pytest.param(start_fields(role=None), "messages_start_role", id="missing-role"),
    pytest.param(start_fields(role="user"), "messages_start_role", id="wrong-role"),
    pytest.param(
        start_fields(content=[{"type": "text", "text": "pre-filled"}]),
        "messages_start_content",
        id="nonempty-start",
    ),
    pytest.param(start_fields(usage={}), "messages_input_tokens", id="missing-input-usage"),
    pytest.param(alter("content_block_delta", index=1), "messages_block_order", id="wrong-index"),
    pytest.param(alter("content_block_start", index=1), "messages_block_order", id="skipped-index"),
    pytest.param(
        alter("content_block_delta", index=True), "messages_block_index", id="boolean-index"
    ),
    pytest.param(
        [message_events()[0], *message_events()[2:]],
        "messages_block_order",
        id="delta-without-start",
    ),
    pytest.param(
        [*message_events()[:2], message_events()[1]], "messages_block_order", id="duplicate-block"
    ),
    pytest.param(
        [*message_events()[:6], message_events()[3]],
        "messages_block_order",
        id="delta-after-block-stop",
    ),
    pytest.param(
        [*message_events()[:5], *message_events()[6:]], "messages_event_order", id="unclosed-block"
    ),
    pytest.param(
        [*message_events()[:7], message_events()[1]],
        "messages_event_order",
        id="block-after-message-delta",
    ),
    pytest.param(
        alter("content_block_delta", delta={"type": "thinking_delta", "thinking": "wrong block"}),
        "messages_delta_block_type",
        id="wrong-delta-type",
    ),
    pytest.param(
        alter("content_block_delta", delta={"type": "text_delta", "text": 42}),
        "messages_delta_text",
        id="invalid-text",
    ),
    pytest.param(
        alter("content_block_delta", delta=None), "messages_block_delta", id="missing-delta"
    ),
    pytest.param(
        alter("message_delta", usage={}), "messages_output_tokens", id="missing-output-usage"
    ),
    pytest.param(
        alter("message_delta", usage={"input_tokens": "wrong", "output_tokens": 2}),
        "messages_input_tokens",
        id="invalid-input-usage-update",
    ),
    pytest.param(
        alter("message_delta", usage={"output_tokens": -1}),
        "messages_output_tokens",
        id="negative-output-usage",
    ),
    pytest.param(
        [
            *message_events()[:-1],
            {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 1}},
            message_events()[-1],
        ],
        "messages_cumulative_usage",
        id="decreasing-usage",
    ),
    pytest.param(
        [message_events()[0], message_events()[-1]], "messages_event_order", id="early-stop"
    ),
    pytest.param(message_events()[:-1], "messages_terminal", id="missing-stop"),
    pytest.param(
        [*message_events(), message_events()[-1]], "messages_after_stop", id="duplicate-stop"
    ),
    pytest.param(
        [
            *message_events()[:3],
            {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
        ],
        "messages_stream_error",
        id="error-event",
    ),
]


@pytest.mark.parametrize(("events", "failed_check"), INVALID)
def test_bad_lifecycles_fail_with_a_journal_entry(events, failed_check):
    checks = journal.activate("messages-negative")
    try:
        with pytest.raises(AssertionError, match=failed_check):
            assert_messages_stream(transcript(events))
        (check,) = checks.checks
        assert check.name == failed_check
        assert not check.ok
    finally:
        journal.deactivate()


@pytest.mark.parametrize(
    ("raw", "failed_check"),
    [
        ("event: ping\ndata: {not json}\n\n", "messages_event_json"),
        ("event: ping\ndata: []\n\n", "messages_event_json"),
        ('event: ping\ndata: {"type":"message_stop"}\n\n', "messages_event_name"),
        ('data: {"type":"message_start"}\n\n', "messages_event_name"),
        ("data: [DONE]\n\n", "messages_event_json"),
        (named_sse(message_events()).rstrip("\n"), "messages_terminal"),
    ],
)
def test_bad_frames_fail_with_evidence(tmp_path, raw, failed_check):
    with FakeFleet(messages_stream=raw) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_messages_streaming")
    assert code == 1
    (record,) = records
    assert record["outcome"] == "fail"
    assert record["http_status"] == 200
    assert any(c["name"] == failed_check and not c["ok"] for c in record["checks"])
    assert raw in (tmp_path / "run" / record["evidence"] / "transcript.sse").read_text()


def test_agent_streams_through_both_ingresses_with_named_event_evidence(tmp_path):
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
            "--client",
            "agent",
            "--local-output",
            str(run_dir),
            "--pod-logs",
            "off",
            "--evidence",
            "all",
            "-k",
            "test_messages_streaming",
        )
    assert result.returncode == 0, result.stdout + result.stderr
    records = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    assert {r["ingress"] for r in records} == {"fake", "router"}
    for record in records:
        assert (record["case_id"], record["surface"], record["outcome"]) == (
            CASE,
            "messages",
            "pass",
        )
        checks = {c["name"]: c for c in record["checks"]}
        assert all(
            checks[name]["ok"]
            for name in (
                "messages_lifecycle",
                "model_echo",
                "finish_reason",
                "output_chars",
                "usage",
            )
        )
        assert checks["messages_lifecycle"]["observed"] == {
            "events": 8,
            "blocks": 1,
            "output_tokens": 2,
        }
        raw = (run_dir / record["evidence"] / "transcript.sse").read_text()
        assert "event: message_start" in raw and "event: message_stop" in raw
        assert "[DONE]" not in raw


@pytest.mark.parametrize(
    ("events", "failed_check"),
    [
        (message_events()[:-1], "messages_terminal"),
        (start_fields(role=None), "messages_start_role"),
        (start_fields(type=None), "messages_start_type"),
        (alter("content_block_delta", index=7), "messages_block_order"),
        (alter("message_delta", usage={}), "messages_output_tokens"),
        (message_events(stop_reason="abort"), "finish_reason"),
        (message_events(stop_reason=None), "finish_reason_recognised"),
        (start_fields(model="wrong/model"), "model_echo"),
        (
            [*message_events()[:3], {"type": "error", "error": {"message": "busy"}}],
            "messages_stream_error",
        ),
    ],
)
def test_http_200_does_not_hide_a_broken_stream(tmp_path, events, failed_check):
    with FakeFleet(messages_stream=named_sse(events)) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_messages_streaming")
    assert code == 1
    (record,) = records
    assert record["outcome"] == "fail"
    assert any(c["name"] == failed_check and not c["ok"] for c in record["checks"])


def test_stream_errors_scrub_secrets_in_the_journal_and_assertion():
    secret = "messages-stream-test-secret"
    redact.install_secrets([secret])
    checks = journal.activate("messages-secret")
    try:
        events = [{"type": "error", "error": {"message": secret}}]
        with pytest.raises(AssertionError) as error:
            assert_messages_stream(transcript(events))
        assert secret not in str(error.value)
        assert secret not in str([c.to_dict() for c in checks.checks])
    finally:
        journal.deactivate()
        redact.reset_secrets()


@pytest.mark.parametrize("error_type", [httpx.RemoteProtocolError, httpx.ReadTimeout])
def test_http_failure_keeps_the_partial_named_transcript_and_idle_timeout(error_type):
    partial = named_sse(message_events()[:2])

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield partial.encode()
            raise error_type("stream interrupted")

    def respond(request):
        assert request.extensions["timeout"]["read"] == 0.5
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=BrokenStream()
        )

    with RecordingClient("http://fake", stream_idle=0.5) as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(respond))
        exchange, stream = client.stream("/v1/messages", json_body={}, stop_event="message_stop")
    assert error_type.__name__ in exchange.transport_error
    assert len(stream.events) == 2
    assert stream.raw == partial
    assert exchange.response_text == partial


def test_streaming_rejects_a_non_sse_content_type(tmp_path):
    with FakeFleet(messages_stream_content_type="application/json") as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_messages_streaming")
    assert code == 1
    (record,) = records
    assert record["outcome"] == "fail"
    assert any(c["name"] == "stream_content_type" and not c["ok"] for c in record["checks"])


def test_messages_streaming_skips_when_not_declared(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet, surfaces=("chat_completions",))
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_messages_streaming")
        assert not fleet.seen
    assert code == 0
    assert (records[0]["outcome"], records[0]["skip_class"]) == ("skip", "undeclared")
