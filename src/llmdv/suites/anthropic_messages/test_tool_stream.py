"""Tool input deltas and signed thinking survive a Messages continuation."""

import pytest

from ...assertions.messages import assert_messages_stream
from ..conftest import budget_for, check_sse_response
from ..tools.helpers import check_tool_completion, check_tool_result, tool_schema


@pytest.mark.surface("messages")
@pytest.mark.needs("tools")
@pytest.mark.case_id("anthropic_messages.streamed_tool_loop")
def test_messages_tool_stream(client, surface, model, ingress):
    schema = tool_schema()
    body = surface.tool_body(model.public_id, schema, budget_for(model, floor=256))
    exchange, transcript = client.stream(
        surface.path, json_body={**body, "stream": True}, stop_event="message_stop"
    )
    check_sse_response(exchange, ingress)
    assert_messages_stream(transcript)
    completion = surface.assemble(
        [surface.parse_chunk(event.json()) for event in transcript.events]
    )
    check_tool_completion(completion, surface, model, schema)
    check_tool_result(client, surface, model, ingress, body, completion)
