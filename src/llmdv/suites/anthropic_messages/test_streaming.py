"""Messages streaming uses named events and message_stop to complete a generation."""

from __future__ import annotations

import pytest

from ...assertions.messages import assert_messages_stream
from ..conftest import budget_for, check_completion, check_sse_response

pytestmark = pytest.mark.surface("messages")


@pytest.mark.case_id("anthropic_messages.streaming_sse")
def test_messages_streaming(client, surface, model, ingress):
    body = surface.stream_body(model.public_id, max_tokens=budget_for(model))
    exchange, transcript = client.stream(surface.path, json_body=body, stop_event="message_stop")
    check_sse_response(exchange, ingress)
    assert_messages_stream(transcript)
    completion = surface.assemble(
        [surface.parse_chunk(event.json()) for event in transcript.events]
    )
    check_completion(completion, surface, model)
