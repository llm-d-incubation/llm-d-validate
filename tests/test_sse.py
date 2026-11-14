"""The SSE reader, exercised against transcripts captured from a live fleet.

Fixtures under tests/fixtures/ are real streams, not hand-written ones, so the parser is held
to what a server actually sends -- including the vendor fields an OpenAI client never reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from llmdv.http import sse

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


# ------------------------------------------------------------------ real streams


def test_reads_a_real_stream_without_usage():
    t = sse.read(fixture("stream-no-usage.sse"))
    assert t.saw_done
    assert len(t.data_events) == 13
    assert all(e.json()["object"] == "chat.completion.chunk" for e in t.data_events)
    assert not [e for e in t.data_events if e.json().get("usage")]


def test_reads_a_real_stream_with_terminal_usage():
    t = sse.read(fixture("stream-with-usage.sse"))
    assert t.saw_done
    usage = [e.json() for e in t.data_events if e.json().get("usage")]
    assert len(usage) == 1
    assert usage[0]["choices"] == [], "the usage frame should carry no choices"
    assert usage[0]["usage"]["total_tokens"] == 22


def test_the_sentinel_is_not_treated_as_data():
    """`[DONE]` carries no JSON; parsing it would fail at the clearest point in the stream."""
    t = sse.read(fixture("stream-with-usage.sse"))
    assert t.events[-1].is_done
    assert len(t.data_events) == len(t.events) - 1
    with pytest.raises(sse.StreamError, match="not valid JSON"):
        t.events[-1].json()


# ------------------------------------------------------------------ the grammar


def test_blank_line_separates_frames():
    t = sse.read(['data: {"a":1}', "", 'data: {"a":2}', "", "data: [DONE]", ""])
    assert [e.json()["a"] for e in t.data_events] == [1, 2]


def test_multi_line_data_is_joined():
    t = sse.read(["data: line one", "data: line two", "", "data: [DONE]"])
    assert t.data_events[0].data == "line one\nline two"


def test_keepalive_comments_are_ignored():
    t = sse.read([": keepalive", 'data: {"a":1}', "", ": ping", "data: [DONE]", ""])
    assert len(t.data_events) == 1 and t.saw_done


def test_reading_stops_at_the_sentinel():
    """Anything after [DONE] is not part of the response, and consuming it can hang."""
    t = sse.read(["data: [DONE]", "", 'data: {"never":"read"}'])
    assert t.saw_done and t.data_events == []


def test_a_stream_without_a_sentinel_is_reported_not_raised():
    """Absence of [DONE] is a finding for a case to assert on, not a parser error."""
    t = sse.read(['data: {"a":1}', ""])
    assert not t.saw_done and len(t.data_events) == 1


def test_unknown_vendor_fields_are_tolerated():
    t = sse.read(['data: {"object":"chat.completion.chunk","token_ids":[1,2],"x":null}', ""])
    assert t.data_events[0].json()["token_ids"] == [1, 2]


def test_a_malformed_frame_raises_a_stream_error():
    t = sse.read(["data: {not json", ""])
    with pytest.raises(sse.StreamError, match="event 0 is not valid JSON"):
        t.data_events[0].json()


def test_idle_timeout_names_how_long_and_how_far():
    """A stalled decode is a different failure from a slow one."""
    import time

    def slow():
        yield 'data: {"a":1}'
        yield ""
        time.sleep(0.05)
        yield 'data: {"a":2}'

    with pytest.raises(sse.StreamIdleTimeout, match="after 1 event"):
        list(sse.parse_lines(slow(), idle_timeout=0.01))


# ------------------------------------------------------------------ the cap


def test_clamp_keeps_both_ends():
    """The head holds the opening frames; the tail holds the finish reason and the sentinel."""
    text = "\n".join(f"data: {i}" for i in range(2000))
    out, truncated = sse.clamp(text, 1000)
    assert truncated and len(out) <= 1000 + 200
    assert out.startswith("data: 0")
    assert out.rstrip().endswith("data: 1999")
    assert "bytes dropped from the middle" in out


def test_clamp_is_a_no_op_under_the_cap():
    out, truncated = sse.clamp("short", 1000)
    assert out == "short" and not truncated


def test_clamp_zero_keeps_everything():
    text = "x" * 5000
    out, truncated = sse.clamp(text, 0)
    assert out == text and not truncated


def test_named_events_reset_at_every_frame_and_keep_received_lines():
    raw = (
        ': keepalive\nevent: ping\ndata: {"type":"ping"}\n\n'
        "event: orphan\n\ndata:  café\ndata: two\n\n"
    )
    transcript = sse.read(raw.splitlines())
    assert [e.event for e in transcript.events] == ["ping", None]
    assert transcript.events[1].data == " café\ntwo"
    assert transcript.raw == raw
    assert transcript.bytes_received == len(raw.encode("utf-8"))


def test_named_terminal_stops_without_waiting_for_eof():
    def lines():
        yield "event: message_stop"
        yield 'data: {"type":"message_stop"}'
        yield ""
        raise AssertionError("read beyond message_stop")

    transcript = sse.read(lines(), stop_event="message_stop")
    assert not transcript.saw_done
    assert transcript.events[-1].event == "message_stop"


def test_eof_does_not_dispatch_an_unfinished_terminal_frame():
    transcript = sse.read(["event: message_stop", 'data: {"type":"message_stop"}'])
    assert not transcript.events
    assert "event: message_stop" in transcript.raw


def test_unicode_transcripts_are_capped_in_bytes():
    raw = "begin\n" + "思考" * 1000 + "\nend"
    text, truncated = sse.clamp(raw, 200)
    assert truncated
    assert len(text.encode("utf-8")) <= 200
    assert text.startswith("begin") and text.endswith("end")
