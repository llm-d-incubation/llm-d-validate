"""Stateless Responses streams must deliver their output before the terminal snapshot."""

from __future__ import annotations

import pytest

from ...assertions.responses import (
    assert_response_stream_output,
    assert_responses_stream,
)
from ...surfaces.responses import TERMINAL_EVENTS
from ..conftest import budget_for, check_completion, check_sse_response

pytestmark = pytest.mark.surface("responses")


@pytest.mark.case_id("openai_responses.streaming_sse")
def test_responses_streaming(client, surface, model, ingress):
    body = surface.stream_body(model.public_id, max_tokens=budget_for(model))
    exchange, transcript = client.stream(
        surface.path, json_body=body, stop_event=TERMINAL_EVENTS | {"error"}
    )
    check_sse_response(exchange, ingress)
    assert_responses_stream(transcript, item_identity=True)
    completion = surface.assemble(
        [surface.parse_chunk(event.json()) for event in transcript.data_events]
    )
    check_completion(completion, surface, model)
    assert_response_stream_output(completion, surface.parse_completion(completion.raw))
