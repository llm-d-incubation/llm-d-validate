"""Conversation content real clients produce that no single-field case sends."""

from __future__ import annotations

import pytest

from ...evidence import journal
from ...surfaces.base import DEFAULT_PROMPT
from ...surfaces.conversation import Conversation
from ..conftest import budget_for, check_basic_completion


@pytest.mark.surface_generic
@pytest.mark.needs("scenario_checks:blank_turns")
@pytest.mark.case_id("scenarios.blank_turns")
def test_blank_turns(client, surface, model, ingress):
    """Empty and whitespace-only earlier turns get an answer or a clean refusal.

    Agents produce them: an empty tool output becomes an empty turn, and a cancelled reply
    leaves whitespace. A 4xx in the surface's envelope is a fine answer; a 5xx is not.
    """
    if not hasattr(surface, "conversation_body"):
        pytest.skip(f"[not_implemented] {surface.name} carries no conversation history")
    conversation = (
        Conversation().user("").assistant(" ").user("\n\t ").assistant("").user(DEFAULT_PROMPT)
    )
    body = surface.conversation_body(model.public_id, conversation, budget_for(model))
    exchange = client.post(surface.path, json_body=body)
    status = exchange.status
    answered = status == 200
    refused = status is not None and 400 <= status < 500
    journal.record("blank_turns_status", status, "200, or a 4xx", ok=answered or refused)
    assert answered or refused, f"blank turns got {status}, expected 200 or a 4xx"
    if answered:
        check_basic_completion(exchange, surface, model, ingress)
        return
    envelope = surface.parse_error(exchange.response_body)
    journal.record(
        "blank_turns_error_envelope",
        envelope.type if envelope is not None else None,
        f"a {surface.name} error envelope",
        ok=envelope is not None,
    )
    assert envelope is not None, f"the {status} refusal carries no {surface.name} error envelope"
