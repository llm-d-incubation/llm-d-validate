"""How a generation ends: at a client's stop sequence, or at its token limit, and saying so."""

import pytest

from ...assertions.anthropic import assert_stopped_at
from ...assertions.messages import assert_messages_stream
from ...evidence import journal
from ..conftest import (
    budget_for,
    check_basic_completion,
    check_completion,
    check_identity,
    check_json_response,
    check_sse_response,
)

pytestmark = pytest.mark.surface("messages")

COUNT_PROMPT = "Count from 1 to 12, separated by commas and spaces. Reply with the numbers only."
# Inside the count, so a model that honours the sequence stops after six.
SEQUENCE = ", 7"


@pytest.mark.needs("messages_checks:stop_sequences")
@pytest.mark.case_id("anthropic_messages.stop_sequence")
@pytest.mark.parametrize("mode", ["message", "stream"])
def test_stop_sequence_ends_and_names_the_turn(client, surface, model, ingress, mode):
    budget = budget_for(model, floor=256)
    if mode == "message":
        body = surface.minimal_body(
            model.public_id, budget, COUNT_PROMPT, stop_sequences=[SEQUENCE]
        )
        completion = check_basic_completion(
            client.post(surface.path, json_body=body), surface, model, ingress
        )
        stop_sequence = completion.raw.get("stop_sequence")
    else:
        body = surface.stream_body(
            model.public_id, budget, prompt=COUNT_PROMPT, stop_sequences=[SEQUENCE]
        )
        exchange, transcript = client.stream(
            surface.path, json_body=body, stop_event="message_stop"
        )
        check_sse_response(exchange, ingress)
        assert_messages_stream(transcript)
        completion = surface.assemble(
            [surface.parse_chunk(event.json()) for event in transcript.events]
        )
        check_completion(completion, surface, model)
        deltas = [e for e in completion.raw["events"] if e.get("type") == "message_delta"]
        stop_sequence = (deltas[-1].get("delta") or {}).get("stop_sequence") if deltas else None
    assert_stopped_at(completion, SEQUENCE, stop_sequence)


@pytest.mark.needs("messages_checks:max_tokens_limit")
@pytest.mark.case_id("anthropic_messages.max_tokens_stop")
def test_max_tokens_one_stops_at_the_limit(client, surface, model, ingress):
    body = surface.minimal_body(model.public_id, 1, COUNT_PROMPT)
    completion = surface.parse_completion(
        check_json_response(client.post(surface.path, json_body=body), ingress)
    )
    check_identity(completion, surface, model)
    output = completion.usage.completion_tokens
    observed = {"stop_reason": completion.finish_reason, "output_tokens": output}
    ok = completion.finish_reason == "max_tokens" and type(output) is int and 0 <= output <= 1
    journal.record("max_tokens_stop", observed, "stop_reason max_tokens, at most 1 token", ok=ok)
    assert ok, f"max_tokens 1 ended with {observed}"
