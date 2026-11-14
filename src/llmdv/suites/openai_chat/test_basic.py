"""The simplest thing that must work: one non-streaming chat completion.

Everything else in the OpenAI surface builds on this, so it runs through every ingress the
fleet declares -- which is also what makes the first attribution column meaningful.

Two assertions here are deliberately not the obvious ones, and both come from measured
fleet behaviour:

`finish_reason` is checked against "abort" explicitly, because a draining replica rejects work
through `_reject_add_in_shutdown`, which surfaces as finish_reason "abort" on an HTTP **200** --
so a status-only check scores a dropped request as a success.

Output is asserted as "content OR reasoning", never "content is non-empty". A fleet served
without --reasoning-parser emits its reasoning as a literal <think> block inside content and
can spend the whole budget there; asserting non-empty content fails a healthy stack, and
asserting a populated reasoning_content field fails a different healthy stack.
"""

from __future__ import annotations

import pytest

from ..conftest import budget_for, check_basic_completion, check_completion, throttle_aware

pytestmark = pytest.mark.surface("chat_completions")


@pytest.mark.case_id("openai_chat.basic_non_streaming")
def test_chat_basic(client, surface, model, ingress):
    """A minimal non-streaming completion returns a well-formed, non-aborted generation."""
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model))
    exchange = client.post(surface.path, json_body=body)
    check_basic_completion(exchange, surface, model, ingress)


@pytest.mark.case_id("openai_chat.basic_parses_with_openai_sdk")
def test_chat_basic_parses_with_pinned_openai_sdk(openai_client, surface, model, ingress):
    """The same exchange deserialises through the pinned OpenAI SDK."""
    with throttle_aware(ingress):
        completion = openai_client.chat.completions.create(
            model=model.public_id,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=budget_for(model),
            temperature=0,
        )
    check_completion(surface.parse_completion(completion.to_dict()), surface, model)
