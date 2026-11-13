"""Completions response and chunk fields against the published types, where declared.

Harnesses that drive the legacy prompt API parse `text_completion` objects with generated
types, so every documented field is checked once per shape, in JSON and in a stream.
"""

from __future__ import annotations

import pytest

from ...assertions.openai_contract import (
    assert_completion_chunks,
    assert_completion_response_fields,
)
from ..conftest import budget_for, check_basic_completion, check_completion
from ..openai_contract import assemble_stream, require_contract, stream_frames

pytestmark = pytest.mark.surface("completions")


@pytest.mark.case_id("openai_completions.response_fields")
def test_completions_response_fields(client, surface, model, ingress):
    require_contract(ingress, surface)
    body = surface.minimal_body(model.public_id, budget_for(model), surface.reply_prompt)
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    assert_completion_response_fields(completion.raw)


@pytest.mark.case_id("openai_completions.stream_chunk_fields")
def test_completions_stream_chunk_fields(client, surface, model, ingress):
    require_contract(ingress, surface)
    body = surface.stream_body(
        model.public_id, budget_for(model), include_usage=True, prompt=surface.reply_prompt
    )
    frames = stream_frames(client, surface, body, ingress)
    assert_completion_chunks(frames)
    check_completion(assemble_stream(surface, frames), surface, model)
