"""Logprobs cover every completion token, with exactly the requested alternatives.

Calibration and classifier clients align logprob entries with tokens. A layer that drops the
logprobs of one chunk, or caps the alternatives, gives them misaligned scores on HTTP 200.
"""

from __future__ import annotations

import pytest

from ...assertions.token_logprobs import assert_token_entries
from ..conftest import check_completion
from ..openai_contract import (
    assemble_stream,
    check_generation,
    require_contract,
    stream_frames,
)

pytestmark = pytest.mark.surface("chat_completions")

WIDTH = 3


@pytest.mark.case_id("openai_chat.logprobs_per_token")
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_chat_logprobs_per_token(client, surface, model, ingress, stream):
    require_contract(ingress, surface)
    if "logprobs" not in ingress.capabilities.chat_options:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare Chat logprobs")
    options = {"logprobs": True, "top_logprobs": WIDTH}
    if stream:
        body = surface.stream_body(model.public_id, 12, include_usage=True, **options)
        frames = stream_frames(client, surface, body, ingress)
        completion = assemble_stream(surface, frames)
        check_completion(completion, surface, model)
        entries = [
            entry
            for frame in frames
            for choice in frame.get("choices") or []
            for entry in ((choice.get("logprobs") or {}).get("content") or [])
        ]
    else:
        body = surface.minimal_body(model.public_id, 12, **options)
        completion = check_generation(
            client.post(surface.path, json_body=body), surface, model, ingress
        )
        entries = (completion.raw["choices"][0].get("logprobs") or {}).get("content")
    assert_token_entries(entries, completion.usage.completion_tokens, WIDTH)
