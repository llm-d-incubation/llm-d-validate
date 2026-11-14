"""A streamed function invocation must reconstruct and complete its next turn."""

import pytest

from ...assertions.chat_choices import assert_choice_stream
from ...assertions.chat_tools import assert_chat_tool_deltas
from ...evidence import journal
from ..conftest import budget_for, check_sse_response
from ..tools.helpers import check_tool_completion, check_tool_result, tool_schema


@pytest.mark.surface("chat_completions")
@pytest.mark.needs("tools")
@pytest.mark.case_id("openai_chat.streamed_tool_loop")
def test_streamed_tool_loop(client, surface, model, ingress):
    schema = tool_schema()
    body = surface.tool_stream_body(model.public_id, schema, budget_for(model, floor=256))
    exchange, transcript = client.stream(surface.path, json_body=body)
    check_sse_response(exchange, ingress)
    journal.record("terminal_done", transcript.saw_done, True, ok=transcript.saw_done)
    assert transcript.saw_done, "tool stream ended without [DONE]"
    frames = [event.json() for event in transcript.data_events]
    assert_choice_stream(frames, 1, model.public_id)
    assert_chat_tool_deltas(frames)
    completion = surface.assemble([surface.parse_chunk(frame) for frame in frames])
    check_tool_completion(completion, surface, model, schema)
    followup = {
        key: value for key, value in body.items() if key not in ("stream", "stream_options")
    }
    check_tool_result(client, surface, model, ingress, followup, completion)
