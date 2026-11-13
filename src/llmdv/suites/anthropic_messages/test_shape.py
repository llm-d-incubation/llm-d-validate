"""The native Message a strict client reads, and the stream that must agree with it."""

import pytest
from anthropic.types import Message

from ...assertions.anthropic import assert_message_shape, input_total
from ...assertions.messages import assert_messages_stream
from ...evidence import journal
from ..conftest import (
    budget_for,
    check_basic_completion,
    check_completion,
    check_sse_response,
    throttle_aware,
)

pytestmark = [pytest.mark.surface("messages"), pytest.mark.needs("messages_checks:response_shape")]


def _answer_blocks(blocks) -> list:
    # Adaptive thinking may appear in one answer and not the other; the answer must not differ.
    return [
        b.get("type")
        for b in blocks
        if isinstance(b, dict) and b.get("type") not in ("thinking", "redacted_thinking")
    ]


@pytest.mark.case_id("anthropic_messages.response_shape")
def test_message_fields_are_native(client, surface, model, ingress):
    body = surface.minimal_body(model.public_id, budget_for(model))
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    assert_message_shape(completion.raw)


@pytest.mark.case_id("anthropic_messages.stream_matches_message")
def test_stream_matches_message(client, surface, model, ingress):
    body = surface.minimal_body(model.public_id, budget_for(model))
    message = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    exchange, transcript = client.stream(
        surface.path, json_body={**body, "stream": True}, stop_event="message_stop"
    )
    check_sse_response(exchange, ingress)
    assert_messages_stream(transcript)
    streamed = surface.assemble([surface.parse_chunk(event.json()) for event in transcript.events])
    check_completion(streamed, surface, model)
    start = next(e for e in streamed.raw["events"] if e.get("type") == "message_start")
    observed = {
        "message": {
            "stop_reason": message.finish_reason,
            "input_tokens": input_total(message.raw.get("usage")),
            "answer_blocks": _answer_blocks(message.raw.get("content") or []),
        },
        "stream": {
            "stop_reason": streamed.finish_reason,
            "input_tokens": input_total((start.get("message") or {}).get("usage")),
            "answer_blocks": _answer_blocks(streamed.raw["content"]),
        },
    }
    ok = observed["message"] == observed["stream"] and bool(observed["stream"]["input_tokens"])
    journal.record(
        "stream_matches_message",
        observed,
        "the same stop reason, answer blocks and input tokens, counted at message_start",
        ok=ok,
    )
    assert ok, "the stream and the message disagree for the same request"


@pytest.mark.case_id("anthropic_messages.stream_parses_with_anthropic_sdk")
def test_stream_accumulates_with_pinned_anthropic_sdk(anthropic_client, surface, model, ingress):
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model))
    extra_body = {"temperature": body.pop("temperature")}
    with throttle_aware(ingress):
        with anthropic_client.messages.stream(**body, extra_body=extra_body) as stream:
            message = stream.get_final_message()
    journal.record(
        "sdk_response_type", type(message).__name__, "Message", ok=isinstance(message, Message)
    )
    assert isinstance(message, Message), "pinned Anthropic SDK did not accumulate a Message"
    check_completion(surface.parse_completion(message.to_dict()), surface, model)
