"""Responses through the pinned OpenAI SDK, which typed clients use, streamed and not."""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...evidence import journal
from ...surfaces.base import DEFAULT_PROMPT
from ...surfaces.responses import TERMINAL_EVENTS
from ..conftest import budget_for, check_completion, throttle_aware

pytestmark = pytest.mark.surface("responses")


@pytest.mark.case_id("openai_responses.sdk_parse")
@pytest.mark.parametrize("mode", ["json", "sse"])
def test_responses_parse_with_pinned_openai_sdk(openai_client, surface, model, ingress, mode):
    """The SDK returns a typed Response, or typed events ending in a terminal response."""
    request = {
        "model": model.public_id,
        "input": DEFAULT_PROMPT,
        "max_output_tokens": budget_for(model),
        "temperature": 0,
        "store": False,
    }
    with throttle_aware(ingress):
        if mode == "json":
            response = openai_client.responses.create(**request)
            kinds = ["response"]
        else:
            kinds = []
            response = None
            with openai_client.responses.create(**request, stream=True) as stream:
                for event in stream:
                    kind = getattr(event, "type", None)
                    kinds.append(kind)
                    if kind in TERMINAL_EVENTS:
                        response = getattr(event, "response", None)
                        break
    ok = kinds[-1] in TERMINAL_EVENTS | {"response"} and response is not None
    journal.record(
        "sdk_events", {"count": len(kinds), "last": kinds[-1]}, "a terminal response", ok=ok
    )
    if not ok:
        fail("sdk_events", kinds[-3:], "a stream ending in a terminal response")
    typed = type(response).__name__ == "Response" and isinstance(response.output_text, str)
    journal.record("sdk_response_type", type(response).__name__, "Response", ok=typed)
    if not typed:
        fail("sdk_response_type", type(response).__name__, "Response with output_text")
    check_completion(surface.parse_completion(response.to_dict()), surface, model)
