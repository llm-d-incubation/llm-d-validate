"""The Response object a typed client parses, and the incomplete status at a token cap."""

from __future__ import annotations

from typing import Any

import pytest

from ...assertions import assert_usage_present_and_consistent, fail
from ...assertions.response_object import assert_response_object
from ...evidence import journal
from ...surfaces.responses import TERMINAL_EVENTS
from ..conftest import (
    budget_for,
    check_completion,
    check_identity,
    check_json_response,
    check_sse_response,
)

pytestmark = pytest.mark.surface("responses")

# Long enough that no model finishes it inside the cap, reasoning or not.
LONG_PROMPT = "Count from 1 to 200, one number per line."
CAP = 16


def _response(client, surface, ingress, body: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode == "json":
        return check_json_response(client.post(surface.path, json_body=body), ingress)
    exchange, transcript = client.stream(
        surface.path, json_body={**body, "stream": True}, stop_event=TERMINAL_EVENTS | {"error"}
    )
    check_sse_response(exchange, ingress)
    surface.assert_stream(transcript)
    terminal = transcript.data_events[-1].json()
    journal.record("terminal_event", terminal.get("type"), "the terminal event name")
    return terminal.get("response") or {}


@pytest.mark.case_id("openai_responses.response_object")
@pytest.mark.parametrize("mode", ["json", "sse"])
def test_response_object_fields(client, surface, model, ingress, mode):
    """Every field the pinned SDK requires, with unique item ids and usage details."""
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model))
    response = _response(client, surface, ingress, body, mode)
    check_completion(surface.parse_completion(response), surface, model)
    assert_response_object(response)


@pytest.mark.case_id("openai_responses.incomplete")
@pytest.mark.parametrize("mode", ["json", "sse"])
def test_token_cap_reports_incomplete(client, surface, model, ingress, mode):
    """A reached cap gives status incomplete for max_output_tokens, within the cap."""
    body = surface.minimal_body(model.public_id, max_tokens=CAP, prompt=LONG_PROMPT)
    response = _response(client, surface, ingress, body, mode)
    completion = surface.parse_completion(response)
    surface.native_contract(response)
    check_identity(completion, surface, model)
    assert_usage_present_and_consistent(completion)
    details = response.get("incomplete_details")
    observed = (
        response.get("status"),
        details.get("reason") if isinstance(details, dict) else None,
    )
    expected = ("incomplete", "max_output_tokens")
    journal.record("incomplete_status", observed, expected, ok=observed == expected)
    if observed != expected:
        fail("incomplete_status", observed, expected)
    used = completion.usage.completion_tokens
    within = isinstance(used, int) and used <= CAP
    journal.record("output_tokens_within_cap", used, f"at most {CAP}", ok=within)
    if not within:
        fail("output_tokens_within_cap", used, f"at most {CAP}")
