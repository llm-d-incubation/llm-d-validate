"""Chat response and chunk fields against the published types, where an ingress opts in.

The ordinary smoke cases read the fields llmdv needs. A strict client reads every documented
field, and an SDK generated from the OpenAI schema in another language rejects a response
whose `created` is in milliseconds, whose finish reason is outside the enum, or whose usage
chunk carries a choice. These cases check each documented field once per shape.
"""

from __future__ import annotations

import pytest

from ...assertions.openai_contract import assert_chat_chunks, assert_chat_response_fields
from ..conftest import budget_for, check_basic_completion, check_choice, check_completion
from ..openai_contract import assemble_stream, require_contract, stream_frames

pytestmark = pytest.mark.surface("chat_completions")

REASONING_FIELDS = ("reasoning", "reasoning_content")


@pytest.mark.case_id("openai_chat.response_fields")
def test_chat_response_fields(client, surface, model, ingress):
    require_contract(ingress, surface)
    body = surface.minimal_body(model.public_id, budget_for(model))
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    assert_chat_response_fields(completion.raw)


@pytest.mark.case_id("openai_chat.stream_chunk_fields")
@pytest.mark.parametrize("mode", ["plain", "usage", "usage_off"])
def test_chat_stream_chunk_fields(client, surface, model, ingress, mode):
    """`usage_off` sends `include_usage: false`, which must stream no usage chunk."""
    require_contract(ingress, surface)
    include_usage = mode == "usage"
    body = surface.stream_body(model.public_id, budget_for(model), include_usage=include_usage)
    if mode == "usage_off":
        body["stream_options"] = {"include_usage": False}
    frames = stream_frames(client, surface, body, ingress)
    assert_chat_chunks(frames, REASONING_FIELDS, include_usage)
    completion = assemble_stream(surface, frames)
    # Usage arrives only when asked for, so the plain stream is judged without it.
    (check_completion if include_usage else check_choice)(completion, surface, model)
