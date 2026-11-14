"""Configured default reasoning and explicitly declared request efforts."""

from __future__ import annotations

import pytest

from ...assertions import assert_answer_only
from ...assertions.chat_reasoning import assert_chat_reasoning
from ...evidence import journal
from ...http.client import RecordingClient
from ...profile.schema import CHAT_REASONING_EFFORTS, Ingress, Model
from ...surfaces.base import REASONING_ANSWER, Surface
from ..conftest import budget_for, check_basic_completion, check_completion, check_sse_response

pytestmark = pytest.mark.surface("chat_completions")


def _check(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    effort: str | None = None,
    stream: bool = False,
) -> None:
    body = surface.reasoning_body(model.public_id, budget_for(model, floor=256), effort, stream)
    journal.record("requested_reasoning_effort", effort)
    if stream:
        exchange, transcript = client.stream(surface.path, json_body=body)
        check_sse_response(exchange, ingress)
        surface.assert_stream(transcript)
        completion = surface.assemble(
            [surface.parse_chunk(e.json()) for e in transcript.data_events]
        )
        check_completion(completion, surface, model)
    else:
        completion = check_basic_completion(
            client.post(surface.path, json_body=body), surface, model, ingress
        )
    assert_chat_reasoning(completion, model, enabled=effort != "none")
    assert_answer_only(completion, model, REASONING_ANSWER)


@pytest.mark.needs("reasoning")
@pytest.mark.case_id("openai_chat.configured_reasoning")
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_configured_reasoning(client, surface, model, ingress, stream):
    if not model.reasoning.default_on:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not enable reasoning by default")
    _check(client, surface, model, ingress, stream=stream)


@pytest.mark.needs("reasoning")
@pytest.mark.case_id("openai_chat.reasoning_effort")
@pytest.mark.parametrize("effort", CHAT_REASONING_EFFORTS)
def test_reasoning_effort(client, surface, model, ingress, effort):
    if effort not in ingress.capabilities.chat_reasoning_efforts:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare Chat effort {effort!r}")
    _check(client, surface, model, ingress, effort)
