"""Optional Responses features are honored, ignored or refused as declared; never a 5xx.

`capabilities.responses_features` maps each feature to `accepted`, `ignored` or `rejected`.
An undeclared feature may be refused with a clean 4xx or answered with a healthy generation.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ...assertions import (
    assert_answer_only,
    assert_client_error,
    assert_error_envelope,
    assert_has_answer,
    assert_not_throttled,
    fail,
)
from ...evidence import journal
from ...profile.schema import RESPONSES_FEATURES
from ...surfaces.responses import TERMINAL_EVENTS
from ...surfaces.responses_features import ANY_STRING, CODE_WORD, FEATURES
from ..conftest import budget_for, check_completion, check_json_response, check_sse_response

pytestmark = pytest.mark.surface("responses")


def _matches(observed: Any, expected: Any) -> bool:
    if expected is ANY_STRING:
        return isinstance(observed, str)
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(
            _matches(observed.get(key), value) for key, value in expected.items()
        )
    if isinstance(expected, bool):
        return observed is expected
    return observed == expected and not isinstance(observed, bool)


def _reasoning_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    output = response.get("output")
    items = output if isinstance(output, list) else []
    return [i for i in items if isinstance(i, dict) and i.get("type") == "reasoning"]


def _check_effect(
    effect: str, response: dict[str, Any], completion, model, max_tokens: int
) -> None:
    if effect == "code_word":
        assert_has_answer(completion, max_tokens)
        assert_answer_only(completion, model, CODE_WORD)
        return
    items = _reasoning_items(response)
    if effect == "summary":
        observed = [i.get("summary") for i in items]
        ok = all(isinstance(s, list) and s for s in observed)
    else:
        observed = [i.get("encrypted_content") for i in items]
        ok = all(isinstance(value, str) and value for value in observed)
    journal.record(f"feature_{effect}", observed, f"{effect} on every reasoning item", ok=ok)
    if not ok:
        fail(f"feature_{effect}", observed, f"{effect} on every reasoning item")


@pytest.mark.case_id("openai_responses.request_feature")
@pytest.mark.parametrize("feature", RESPONSES_FEATURES)
def test_responses_request_feature(client, surface, model, ingress, feature):
    spec = FEATURES[feature]
    disposition = ingress.capabilities.responses_features.get(feature)
    journal.record("feature_disposition", disposition or "undeclared", feature)
    marker = uuid.uuid4().hex[:12]
    budget = budget_for(model, floor=128)
    # Added after the builder, whose own `prompt` argument is the input text.
    body = {
        **surface.minimal_body(model.public_id, budget),
        **spec.body(marker),
    }
    if spec.stream:
        exchange, transcript = client.stream(
            surface.path, json_body=body, stop_event=TERMINAL_EVENTS | {"error"}
        )
    else:
        exchange, transcript = client.post(surface.path, json_body=body), None
    assert_not_throttled(exchange, ingress)
    if exchange.transport_error is not None:
        fail("feature_transport", exchange.transport_error, "an HTTP response")
    status = exchange.status
    clean = status is not None and status < 500
    journal.record("feature_status", status, "a 2xx or a clean 4xx, never a 5xx", ok=clean)
    if not clean:
        fail("feature_status", status, "a 2xx or a clean 4xx, never a 5xx")

    if disposition == "rejected" or (disposition is None and 400 <= status < 500):
        served = surface.looks_like_success(exchange.response_body)
        journal.record("feature_generation", served, "no generation", ok=not served)
        if served:
            fail("feature_generation", "a generation", f"{feature} refused")
        assert_client_error(exchange, ingress, "unsupported_feature")
        assert_error_envelope(exchange, surface, ingress, "unsupported_feature")
        return

    if transcript is None:
        response = check_json_response(exchange, ingress)
        completion = surface.parse_completion(response)
    else:
        check_sse_response(exchange, ingress)
        surface.assert_stream(transcript)
        frames = [event.json() for event in transcript.data_events]
        completion = surface.assemble([surface.parse_chunk(frame) for frame in frames])
        response = completion.raw
    check_completion(completion, surface, model)
    output = response.get("output") if isinstance(response.get("output"), list) else []
    ran = [i.get("type") for i in output if isinstance(i, dict) and i.get("type") in spec.calls]
    if disposition == "ignored":
        journal.record("feature_ignored", ran, f"no {feature} call items", ok=not ran)
        if ran:
            fail("feature_ignored", ran, f"no {feature} call items for an ignored tool")
    if disposition != "accepted":
        return
    if spec.echo is not None:
        expected = spec.expected(marker)
        observed = response.get(spec.echo)
        ok = _matches(observed, expected)
        shown = "a string" if expected is ANY_STRING else expected
        journal.record("feature_echo", {spec.echo: observed}, {spec.echo: shown}, ok=ok)
        if not ok:
            fail("feature_echo", {spec.echo: observed}, {spec.echo: shown})
    if spec.effect == "no_obfuscation":
        padded = sum("obfuscation" in event.json() for event in transcript.data_events)
        journal.record("stream_obfuscation", padded, "no obfuscation field", ok=not padded)
        if padded:
            fail("stream_obfuscation", padded, "no obfuscation field on any event")
    elif spec.effect is not None:
        _check_effect(spec.effect, response, completion, model, budget)
