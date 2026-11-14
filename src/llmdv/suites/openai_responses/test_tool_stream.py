"""Function argument deltas must match both done events and the terminal snapshot."""

import pytest

from ...assertions.responses import assert_response_stream_output, assert_responses_stream
from ...surfaces.responses import TERMINAL_EVENTS
from ..conftest import budget_for, check_sse_response
from ..tools.helpers import check_tool_completion, check_tool_result, tool_schema


@pytest.mark.surface("responses")
@pytest.mark.needs("tools")
@pytest.mark.case_id("openai_responses.streamed_tool_loop")
def test_responses_tool_stream(client, surface, model, ingress):
    schema = tool_schema()
    body = surface.tool_body(model.public_id, schema, budget_for(model, floor=256))
    exchange, transcript = client.stream(
        surface.path, json_body={**body, "stream": True}, stop_event=TERMINAL_EVENTS | {"error"}
    )
    check_sse_response(exchange, ingress)
    assert_responses_stream(transcript, item_identity=True)
    completion = surface.assemble(
        [surface.parse_chunk(event.json()) for event in transcript.data_events]
    )
    assert_response_stream_output(completion, surface.parse_completion(completion.raw))
    check_tool_completion(completion, surface, model, schema)
    check_tool_result(client, surface, model, ingress, body, completion)
