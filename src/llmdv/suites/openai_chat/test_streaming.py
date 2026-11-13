"""Streamed chat completions: the frame grammar, and terminal usage.

Streaming is where a client spends its time, and it fails differently from a single response.
A stack can return a well-formed final answer while emitting frames no SDK can parse, omitting
the terminal sentinel so a reader hangs, or stalling mid-decode on a connection that is still
open. None of that is visible to a non-streaming case.

The assembled result is pushed through the SAME assertions a non-streaming completion gets --
not aborted, produced output, usage self-consistent -- so the two paths cannot drift. What is
asserted here on top of that is the grammar itself.
"""

from __future__ import annotations

import pytest

from ...assertions import assert_has_output, assert_healthy_finish
from ...evidence import journal
from ..conftest import budget_for, check_model_echo, check_sse_response

pytestmark = pytest.mark.surface("chat_completions")


@pytest.mark.case_id("openai_chat.streaming_sse")
def test_streaming_sse(client, surface, model, ingress):
    """A streamed completion has valid frames, one finish reason, and a terminal `[DONE]`."""
    body = surface.stream_body(model.public_id, max_tokens=budget_for(model))
    exchange, transcript = client.stream(surface.path, json_body=body)

    check_sse_response(exchange, ingress)

    events = transcript.data_events
    journal.record("sse_events", len(events), "at least one frame", ok=bool(events))
    assert events, "the stream carried no data frames"

    # Every frame must parse. A single unparseable frame breaks any SDK reading the stream,
    # even if the text that arrives either side of it looks fine.
    chunks = []
    for event in events:
        chunks.append(surface.parse_chunk(event.json()))

    journal.record(
        "terminal_done", transcript.saw_done, "stream ends with [DONE]", ok=transcript.saw_done
    )
    assert transcript.saw_done, (
        "stream ended without a terminal [DONE] sentinel; a client reading to the sentinel "
        "would wait for a frame that never arrives"
    )

    finishes = [c.finish_reason for c in chunks if c.finish_reason]
    journal.record("finish_reason_frames", finishes, "exactly one", ok=len(finishes) == 1)
    assert len(finishes) == 1, (
        f"expected exactly one frame carrying a finish reason, got {len(finishes)}: {finishes}"
    )

    completion = surface.assemble(chunks)
    journal.record(
        "object",
        completion.object_type,
        "chat.completion.chunk",
        ok=completion.object_type == "chat.completion.chunk",
    )
    assert completion.object_type == "chat.completion.chunk", (
        f"expected frames of object 'chat.completion.chunk', got {completion.object_type!r}"
    )

    check_model_echo(completion, model)
    assert_healthy_finish(completion)
    assert_has_output(completion, model)


@pytest.mark.case_id("openai_chat.streaming_include_usage")
def test_streaming_include_usage(client, surface, model, ingress):
    """`stream_options.include_usage` adds a terminal usage frame, and nothing else does.

    Both halves matter. A stack that never emits usage leaves a client unable to account for
    tokens; one that emits it unconditionally breaks a client that treats a choice-less frame
    as the end of its content.
    """
    budget = budget_for(model)

    without = surface.stream_body(model.public_id, max_tokens=budget)
    exchange, plain = client.stream(surface.path, json_body=without)
    check_sse_response(exchange, ingress)
    plain_usage = [
        c
        for c in map(surface.parse_chunk, (e.json() for e in plain.data_events))
        if c.usage is not None
    ]
    journal.record("usage_frames_without_flag", len(plain_usage), "none", ok=not plain_usage)
    assert not plain_usage, (
        "usage arrived without stream_options.include_usage; a client that stops at the first "
        "choice-less frame would truncate here"
    )

    asked = surface.stream_body(model.public_id, max_tokens=budget, include_usage=True)
    exchange, transcript = client.stream(surface.path, json_body=asked)
    check_sse_response(exchange, ingress)

    chunks = [surface.parse_chunk(e.json()) for e in transcript.data_events]
    terminal = [c for c in chunks if c.is_terminal_usage]
    journal.record("terminal_usage_frames", len(terminal), "exactly one", ok=len(terminal) == 1)
    assert len(terminal) == 1, (
        f"expected exactly one terminal usage frame (empty choices, populated usage), "
        f"got {len(terminal)}"
    )

    usage = terminal[0].usage
    journal.record(
        "streamed_usage",
        f"{usage.prompt_tokens}+{usage.completion_tokens}={usage.total_tokens}",
        "present and self-consistent",
        ok=usage.is_self_consistent and usage.prompt_tokens is not None,
    )
    assert usage.prompt_tokens is not None, "terminal usage frame has no prompt_tokens"
    assert usage.completion_tokens is not None, "terminal usage frame has no completion_tokens"
    assert usage.is_self_consistent, (
        f"streamed usage does not add up: {usage.prompt_tokens} + {usage.completion_tokens} "
        f"!= {usage.total_tokens}"
    )

    # The usage frame must come after the content is finished, not interleaved with it.
    last_finish = max(i for i, c in enumerate(chunks) if c.finish_reason)
    last_usage = max(i for i, c in enumerate(chunks) if c.is_terminal_usage)
    journal.record(
        "usage_after_finish",
        last_usage > last_finish,
        "usage frame follows the finish reason",
        ok=last_usage > last_finish,
    )
    assert last_usage > last_finish, (
        "the terminal usage frame arrived before the finish reason, so a client that stops at "
        "the finish reason would never see it"
    )
