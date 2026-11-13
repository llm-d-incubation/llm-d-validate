"""Every requested choice is checked, including ones after index zero."""

import pytest

from ...assertions import assert_answer_only, assert_usage_present_and_consistent
from ...assertions.chat_choices import assert_choice_indices, assert_choice_stream
from ...assertions.logprobs import assert_logprobs
from ...evidence import journal
from ...surfaces.base import DEFAULT_ANSWER
from ..conftest import (
    budget_for,
    check_choice,
    check_completion,
    check_json_response,
    check_sse_response,
)

pytestmark = pytest.mark.surface("chat_completions")


@pytest.mark.case_id("openai_chat.multiple_choices")
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_multiple_choices(client, surface, model, ingress, stream):
    if "n" not in ingress.capabilities.chat_options:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare Chat n")
    # vLLM permits only one choice with greedy sampling; this case checks shape, not exact text.
    if stream:
        body = surface.stream_body(
            model.public_id, budget_for(model), include_usage=True, n=2, temperature=1
        )
        exchange, transcript = client.stream(surface.path, json_body=body)
        check_sse_response(exchange, ingress)
        journal.record("terminal_done", transcript.saw_done, True, ok=transcript.saw_done)
        assert transcript.saw_done, "multiple-choice stream ended without [DONE]"
        frames = [event.json() for event in transcript.data_events]
        assert_choice_stream(frames, 2, model.public_id)
        completions = surface.assemble_choices(frames, 2)
    else:
        body = surface.minimal_body(model.public_id, budget_for(model), n=2, temperature=1)
        response = check_json_response(client.post(surface.path, json_body=body), ingress)
        assert_choice_indices(response.get("choices"), 2)
        completions = surface.parse_completions(response)
    assert_usage_present_and_consistent(completions[0])
    for index, completion in enumerate(completions):
        journal.record("checking_choice", index)
        check_choice(completion, surface, model)
        assert_answer_only(completion, model, DEFAULT_ANSWER)


@pytest.mark.case_id("openai_chat.output_logprobs")
def test_chat_output_logprobs(client, surface, model, ingress):
    if "logprobs" not in ingress.capabilities.chat_options:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare Chat logprobs")
    body = surface.minimal_body(model.public_id, budget_for(model), logprobs=True, top_logprobs=2)
    response = check_json_response(client.post(surface.path, json_body=body), ingress)
    assert_choice_indices(response.get("choices"), 1)
    check_completion(surface.parse_completion(response), surface, model)
    raw = response["choices"][0].get("logprobs")
    assert_logprobs([raw.get("content") if isinstance(raw, dict) else None], body["top_logprobs"])
