"""SSE as a strict client parses it, and the frame orderings clients depend on.

Each surface's stream case reads decoded lines through the terminal event and stops. These
cases read the raw bytes to EOF, so they also see line terminators, invalid UTF-8, a line cut
at EOF, unknown field names and anything sent after the terminal event, which a client that
reads to EOF parses as one more frame.

`streaming.sentinel_in_content` asks the model to write the OpenAI stream terminator, `[DONE]`,
as text. A layer that looks for that string inside frames, instead of matching the whole
`data: [DONE]` line, ends the stream early and drops the frame's text, while the stream it
forwards stays parseable.
"""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...assertions.frames import (
    assert_chat_order,
    assert_completion_frames,
    assert_nothing_after_terminal,
    assert_sse_lines,
    assert_thinking_first,
)
from ...evidence import journal
from ...http.client import RecordingClient
from ...http.sse import Transcript
from ...profile.schema import Ingress, Model
from ...surfaces.base import Surface
from ..conftest import budget_for, check_sse_response

SENTINEL = "[DONE]"
SENTINEL_LINE = f"alpha {SENTINEL} beta data: {SENTINEL} gamma"
# Several copies make it likely that some frame carries the whole string, which is what a
# substring check needs; a sentinel split across two frames passes such a layer untouched.
SENTINEL_PROMPT = (
    f"Repeat this line five times, one per line, and write nothing else: {SENTINEL_LINE}"
)


def _stream(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    *,
    include_usage: bool = True,
    max_tokens: int | None = None,
    **extra,
) -> Transcript:
    body = surface.stream_body(
        model.public_id,
        max_tokens=max_tokens or budget_for(model),
        include_usage=include_usage,
        **extra,
    )
    exchange, transcript = client.stream(
        surface.path, json_body=body, stop_event=surface.stream_stop, drain=True, framing=True
    )
    check_sse_response(exchange, ingress)
    assert exchange.transport_error is None, f"stream failed: {exchange.transport_error}"
    return transcript


@pytest.mark.surface_generic()
@pytest.mark.case_id("streaming.sse_framing")
def test_sse_framing(client, surface, model, ingress):
    """Every line is a known SSE field, a comment or blank; bytes are UTF-8; no partial line."""
    assert_sse_lines(_stream(client, surface, model, ingress))


@pytest.mark.surface_generic()
@pytest.mark.case_id("streaming.after_terminal")
def test_nothing_after_terminal(client, surface, model, ingress):
    """The response ends cleanly at the surface's terminal event, with nothing after it."""
    assert_nothing_after_terminal(_stream(client, surface, model, ingress))


@pytest.mark.surface("chat_completions")
@pytest.mark.case_id("streaming.frame_order")
def test_chat_frame_order(client, surface, model, ingress):
    """The first choice frame carries the role; reasoning deltas all precede the answer."""
    transcript = _stream(client, surface, model, ingress)
    surface.assert_stream(transcript)
    assert_chat_order(transcript)


@pytest.mark.surface("messages")
@pytest.mark.case_id("streaming.thinking_order")
def test_messages_frame_order(client, surface, model, ingress):
    """With the deployment's default thinking, any thinking block streams before the text."""
    transcript = _stream(client, surface, model, ingress)
    surface.assert_stream(transcript)
    assert_thinking_first(transcript)


@pytest.mark.surface("completions")
@pytest.mark.parametrize("include_usage", [True, False], ids=["usage", "no-usage"])
@pytest.mark.case_id("streaming.completions_lifecycle")
def test_completions_stream_lifecycle(client, surface, model, ingress, include_usage):
    """A streamed text completion has one finish and optional terminal usage before [DONE]."""
    assert_completion_frames(
        _stream(client, surface, model, ingress, include_usage=include_usage), model.public_id
    )


@pytest.mark.surface_generic()
@pytest.mark.case_id("streaming.sentinel_in_content")
def test_sentinel_in_content(client, surface, model, ingress):
    """Model text containing `[DONE]` neither ends the stream nor loses its text.

    Only the effects of the sentinel are judged, so a surface's unrelated framing defects stay
    with the lifecycle cases. At most one frame finishes (Responses carries its finish in the
    terminal snapshot, so its frames carry none); no text or reasoning follows the first
    finish; and the assembled text still contains `[DONE]`, which catches a layer that drops a
    frame's text without misframing the stream. A model that ignores the prompt fails the text
    check too, so read the transcript before blaming the fleet.

    `sentinel_deltas` records how many frames carried the whole string. A substring check only
    fires on those, so a pass with none did not exercise it.
    """
    budget = budget_for(model, floor=256)
    transcript = _stream(client, surface, model, ingress, max_tokens=budget, prompt=SENTINEL_PROMPT)
    chunks = [surface.parse_chunk(event.json()) for event in transcript.data_events]

    def output(chunk) -> str:
        return chunk.content_delta + chunk.reasoning_delta + getattr(chunk, "summary_delta", "")

    whole = sum(SENTINEL in output(chunk) for chunk in chunks)
    journal.record("sentinel_deltas", whole, "at least one for the probe to exercise a layer")
    finishes = [index for index, chunk in enumerate(chunks) if chunk.finish_reason]
    if len(finishes) > 1:
        fail("finish_frames", finishes, "at most one frame carrying a finish")
    journal.record("finish_frames", finishes, "at most one frame carrying a finish", ok=True)
    first = finishes[0] if finishes else len(chunks)
    late = [index for index in range(first + 1, len(chunks)) if output(chunks[index])]
    if late:
        fail("output_after_finish", late, f"no output after the finish at frame {first}")
    journal.record("output_after_finish", [], "no output after the finish", ok=True)
    kept = surface.assemble(chunks)
    count = (kept.text + kept.reasoning_text).count(SENTINEL)
    expected = f"at least one {SENTINEL} in the text and reasoning"
    if not count:
        fail("sentinel_text", count, expected)
    journal.record("sentinel_text", count, expected, ok=True)
