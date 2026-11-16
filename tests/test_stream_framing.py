"""SSE framing, after-terminal and frame-order cases against correct and defective streams."""

from __future__ import annotations

import json

import httpx
import pytest

from llmdv.assertions.frames import assert_nothing_after_terminal, assert_sse_lines
from llmdv.http import sse
from llmdv.http.client import RecordingClient
from tests.fakes import (
    MODEL,
    FakeFleet,
    message_events,
    named_sse,
    response_body,
    response_events,
)
from tests.transport_helpers import outcomes, run

FRAMING_CASES = (
    "test_sse_framing or test_nothing_after_terminal or test_chat_frame_order "
    "or test_messages_frame_order or test_completions_stream_lifecycle"
)
SURFACES = 4  # chat_completions, completions, messages and responses


def test_framing_cases_pass_on_every_surface(tmp_path):
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, FRAMING_CASES)
    for case in ("streaming.sse_framing", "streaming.after_terminal"):
        records = by_case[case]
        assert {r["outcome"] for r in records} == {"pass"}, records
        assert len(records) == SURFACES
    assert outcomes(by_case, "streaming.frame_order") == {"pass"}
    assert outcomes(by_case, "streaming.thinking_order") == {"pass"}
    assert outcomes(by_case, "streaming.completions_lifecycle") == {"pass"}
    assert code == 0


def test_reasoning_in_a_field_before_the_answer_passes(tmp_path):
    with FakeFleet(transport={"reasoning_field": "reasoning_content"}) as fleet:
        code, by_case = run(tmp_path, fleet, "test_chat_frame_order")
    assert outcomes(by_case, "streaming.frame_order") == {"pass"}
    assert code == 0


@pytest.mark.parametrize(
    "defect,select,case_id,check",
    [
        ("sse_unknown_field", "test_sse_framing", "streaming.sse_framing", "sse_field_lines"),
        ("sse_invalid_utf8", "test_sse_framing", "streaming.sse_framing", "stream_utf8"),
        ("sse_partial_line", "test_sse_framing", "streaming.sse_framing", "partial_line_at_eof"),
        ("after_done_frame", "test_nothing_after_terminal", "streaming.after_terminal",
         "after_terminal"),
        ("role_late", "test_chat_frame_order", "streaming.frame_order", "first_role"),
        ("reasoning_after_content", "test_chat_frame_order", "streaming.frame_order",
         "reasoning_order"),
        ("completions_two_finishes", "test_completions_stream_lifecycle",
         "streaming.completions_lifecycle", "finish_frames"),
        ("completions_error_frame", "test_completions_stream_lifecycle",
         "streaming.completions_lifecycle", "stream_error_frame"),
        ("completions_empty_choice_frame", "test_completions_stream_lifecycle",
         "streaming.completions_lifecycle", "usage_without_request"),
        ("completions_chat_object", "test_completions_stream_lifecycle",
         "streaming.completions_lifecycle", "chunk_object"),
    ],
)  # fmt: skip
def test_stream_defects_fail(tmp_path, defect, select, case_id, check):
    with FakeFleet(transport={"defects": {defect}}) as fleet:
        code, by_case = run(
            tmp_path, fleet, select, surfaces=("chat_completions", "completions"), responses=False
        )
    records = by_case[case_id]
    assert "fail" in {r["outcome"] for r in records}, records
    failed = [c["name"] for r in records for c in r.get("checks", []) if c.get("ok") is False]
    assert check in failed, failed
    assert code == 1


def test_sentinel_in_content_passes_on_every_surface(tmp_path):
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, "test_sentinel_in_content")
    records = by_case["streaming.sentinel_in_content"]
    assert {r["outcome"] for r in records} == {"pass"}, records
    assert len(records) == SURFACES
    assert code == 0


def test_a_layer_ending_the_stream_on_sentinel_text_fails(tmp_path):
    """LiteLLM 1.105 turns a frame whose text contains [DONE] into a finish (tools#414)."""
    with FakeFleet(transport={"defects": {"sentinel_ends_stream"}}) as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_sentinel_in_content",
            surfaces=("chat_completions", "completions"),
            responses=False,
        )
    records = by_case["streaming.sentinel_in_content"]
    assert _failed_checks(records) == {
        "chat_completions": ["finish_frames"],
        "completions": ["finish_frames"],
    }, records
    assert code == 1


def test_output_after_a_single_early_finish_fails(tmp_path):
    """The sentinel frame became the only finish, and the generation kept streaming."""

    def frame(delta, finish=None):
        choice = {"index": 0, "delta": delta, "finish_reason": finish}
        body = {"id": "c", "object": "chat.completion.chunk", "model": MODEL, "choices": [choice]}
        return f"data: {json.dumps(body)}\n\n"

    stream = "".join(
        [
            frame({"role": "assistant", "content": "alpha [DO"}),
            frame({}, finish="stop"),
            frame({"content": "NE] beta"}),
            "data: [DONE]\n\n",
        ]
    ).encode()
    with FakeFleet(transport={"chat_stream": stream}) as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_sentinel_in_content",
            surfaces=("chat_completions",),
            responses=False,
        )
    records = by_case["streaming.sentinel_in_content"]
    assert _failed_checks(records) == {"chat_completions": ["output_after_finish"]}, records
    assert code == 1


def test_a_layer_dropping_sentinel_text_fails_on_the_text_check(tmp_path):
    """A layer that drops the frame's text but keeps the framing intact."""
    with FakeFleet(messages_stream=named_sse(message_events())) as fleet:
        code, by_case = run(
            tmp_path, fleet, "test_sentinel_in_content", surfaces=("messages",), responses=False
        )
    records = by_case["streaming.sentinel_in_content"]
    assert _failed_checks(records) == {"messages": ["sentinel_text"]}, records
    assert code == 1


def _failed_checks(records: list[dict]) -> dict[str, list[str]]:
    """The not-ok checks of each failed record, keyed by surface."""
    return {
        r["nodeid"].rsplit("-", 1)[1].rstrip("]"): [
            c["name"] for c in r.get("checks", []) if c.get("ok") is False
        ]
        for r in records
        if r["outcome"] == "fail"
    }


def test_a_responses_stream_ending_in_done_fails_after_terminal(tmp_path):
    """The chat-style [DONE] after response.completed, as one gateway sends it."""
    stream = named_sse(response_events(response=response_body())) + "data: [DONE]\n\n"
    with FakeFleet(responses_stream=stream) as fleet:
        code, by_case = run(
            tmp_path, fleet, "test_nothing_after_terminal and responses", surfaces=("messages",)
        )
    assert outcomes(by_case, "streaming.after_terminal") == {"fail"}
    assert code == 1


def test_split_lines_records_terminators_and_invalid_utf8():
    stats: dict = {}
    lines = list(sse.split_lines([b"data: a\r\n", b"\r\ndata: \xff", b"b\n\nda"], stats))
    assert lines[:3] == ["data: a", "", "data: \ufffdb"]
    assert stats["crlf"] == 2 and stats["lf"] == 2
    assert stats["utf8_valid"] is False
    assert stats["partial_line_at_eof"] is True


@pytest.mark.parametrize("character", ["\u00e9", "\x85", "\u2028", "\u2029"])
def test_split_lines_joins_a_multibyte_character_across_chunks(character):
    stats: dict = {}
    encoded = f"data: {character}\n\n".encode()
    lines = list(sse.split_lines([encoded[:7], encoded[7:]], stats))
    assert lines == [f"data: {character}", ""]
    assert stats["utf8_valid"] is True and not stats["partial_line_at_eof"]


@pytest.mark.parametrize(
    "chunks,expected,partial",
    [
        ([b"data: \xe2", b"X\n\n"], ["data: \ufffdX", ""], False),
        ([b"data: \xff\xe2", b"\x82\xac\n\n"], ["data: \ufffd\u20ac", ""], False),
        ([b"data: \xe2"], ["data: \ufffd"], True),
    ],
    ids=["buffered-prefix", "trailing-prefix", "eof"],
)
def test_split_lines_preserves_invalid_utf8_across_chunks(chunks, expected, partial):
    stats: dict = {}
    assert list(sse.split_lines(chunks, stats)) == expected
    assert stats["utf8_valid"] is False
    assert stats["partial_line_at_eof"] == partial


def test_drain_keeps_lines_after_the_terminal_event_apart():
    raw = ["data: {}", "", "data: [DONE]", "", 'data: {"late": 1}', ""]
    transcript = sse.read(iter(raw), drain=True)
    assert transcript.saw_done and transcript.terminal_seen
    assert transcript.trailing_lines == ['data: {"late": 1}']
    assert [e.data for e in transcript.trailing] == ['{"late": 1}']
    undrained = sse.read(iter(raw))
    assert undrained.trailing == [] and undrained.trailing_lines == []


def test_drain_limit_reports_that_eof_was_not_observed():
    raw = iter(["data: [DONE]", "", *([""] * 4), 'data: {"unread": true}', ""])
    transcript = sse.read(raw, drain=True, drain_limit=3)
    assert transcript.trailing_lines == []
    assert transcript.trailing == []
    assert transcript.framing["drain_error"] == (
        "drain stopped after 3 lines without observing EOF"
    )
    with pytest.raises(AssertionError, match="after_terminal_read"):
        assert_nothing_after_terminal(transcript)
    assert next(raw) == ""


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_sse_field_check_does_not_treat_unicode_separators_as_lines(separator):
    transcript = sse.Transcript(raw=f'data: {{"text": "before{separator}after"}}\n\n')
    assert_sse_lines(transcript)


@pytest.mark.parametrize("separator", ["\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("framing", [False, True], ids=["default", "framing"])
def test_recording_client_preserves_unicode_separators(separator, framing):
    chunk = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL,
        "choices": [
            {"index": 0, "delta": {"content": f"before{separator}after"}, "finish_reason": None}
        ],
    }
    raw = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
    with FakeFleet(transport={"chat_stream": raw.encode()}) as fleet:
        with RecordingClient(fleet.base_url) as client:
            exchange, transcript = client.stream(
                "/v1/chat/completions",
                json_body={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
                **({"framing": True} if framing else {}),
            )
    assert exchange.status == 200 and exchange.transport_error is None
    assert transcript.saw_done
    assert [event.json() for event in transcript.data_events] == [chunk]
    assert transcript.raw == raw
    assert transcript.bytes_received == len(raw.encode())
    assert exchange.response_text == raw
    assert bool(transcript.framing) == framing


@pytest.mark.parametrize(
    "chunks,content",
    [
        ([b'data: {"content": "\xe2', b'X"}\n\n'], "\ufffdX"),
        ([b'data: {"content": "\xff\xe2', b'\x82\xac"}\n\n'], "\ufffd\u20ac"),
    ],
    ids=["buffered-prefix", "trailing-prefix"],
)
@pytest.mark.parametrize("framing", [None, False, True], ids=["default", "disabled", "framing"])
def test_recording_client_preserves_invalid_utf8_across_chunks(chunks, content, framing):
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield from chunks
            yield b"data: [DONE]\n\n"

    expected = {"content": content}
    raw = f"data: {json.dumps(expected, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
    reference = httpx.Response(200, stream=Chunks())
    assert "\n".join(reference.iter_lines()) + "\n" == raw

    def respond(request):
        return httpx.Response(200, stream=Chunks())

    with RecordingClient("http://test") as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(respond))
        exchange, transcript = client.stream(
            "/v1/chat/completions", **({} if framing is None else {"framing": framing})
        )
    assert exchange.status == 200 and exchange.transport_error is None
    assert transcript.saw_done
    assert [event.json() for event in transcript.data_events] == [expected]
    assert transcript.raw == raw and exchange.response_text == raw
    assert transcript.bytes_received == len(raw.encode())
    if framing:
        assert transcript.framing["utf8_valid"] is False
        assert not transcript.framing["partial_line_at_eof"]
    else:
        assert transcript.framing == {}


def _thinking_stream(thinking_first: bool) -> str:
    events = message_events()
    thinking = [
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "weighing it up"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "sig"}},
        {"type": "content_block_stop", "index": 0},
    ]  # fmt: skip
    text = events[1:6]
    first, second = (thinking, text) if thinking_first else (text, thinking)
    for index, block in enumerate((first, second)):
        for event in block:
            if "index" in event:
                event["index"] = index
    return named_sse([events[0], *first, *second, *events[6:]])


@pytest.mark.parametrize("thinking_first", [True, False])
def test_messages_thinking_order(tmp_path, thinking_first):
    with FakeFleet(messages_stream=_thinking_stream(thinking_first)) as fleet:
        code, by_case = run(
            tmp_path, fleet, "test_messages_frame_order", surfaces=("messages",), responses=False
        )
    assert outcomes(by_case, "streaming.thinking_order") == {"pass" if thinking_first else "fail"}
    assert code == (0 if thinking_first else 1)
