"""A stream that is relayed, not buffered: output deltas arrive spread over time."""

import pytest

from ...assertions.context import assert_incremental
from ...assertions.messages import assert_messages_stream
from ..conftest import budget_for, check_sse_response


@pytest.mark.surface("messages")
@pytest.mark.needs("messages_checks:incremental_streaming")
@pytest.mark.case_id("anthropic_messages.incremental_stream")
def test_stream_is_relayed_as_produced(client, surface, model, ingress):
    body = surface.stream_body(
        model.public_id,
        max_tokens=budget_for(model, floor=512),
        prompt="Count from 1 to 100, one number per line.",
    )
    exchange, transcript = client.stream(surface.path, json_body=body, stop_event="message_stop")
    check_sse_response(exchange, ingress)
    assert_messages_stream(transcript)
    times = []
    for event in transcript.events:
        chunk = surface.parse_chunk(event.json())
        if chunk.content_delta or chunk.reasoning_delta:
            times.append(event.elapsed_s)
    assert_incremental(times)
