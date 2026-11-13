"""Non-streaming Messages through raw HTTP and the pinned Anthropic SDK."""

from __future__ import annotations

import pytest
from anthropic.types import Message

from ...evidence import journal
from ..conftest import budget_for, check_basic_completion, check_completion, throttle_aware

pytestmark = pytest.mark.surface("messages")


@pytest.mark.case_id("anthropic_messages.basic_non_streaming")
def test_messages_basic(client, surface, model, ingress):
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model))
    exchange = client.post(surface.path, json_body=body)
    check_basic_completion(exchange, surface, model, ingress)


@pytest.mark.case_id("anthropic_messages.basic_parses_with_anthropic_sdk")
def test_messages_basic_parses_with_pinned_anthropic_sdk(anthropic_client, surface, model, ingress):
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model))
    # The pinned SDK has no named temperature argument; retain it on the wire via extra_body.
    extra_body = {"temperature": body.pop("temperature")}
    with throttle_aware(ingress):
        message = anthropic_client.messages.create(**body, extra_body=extra_body)
    journal.record(
        "sdk_response_type", type(message).__name__, "Message", ok=isinstance(message, Message)
    )
    assert isinstance(message, Message), "pinned Anthropic SDK did not parse a Message"
    check_completion(surface.parse_completion(message.to_dict()), surface, model)
