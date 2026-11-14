"""Native Responses options with observable response contracts."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ...assertions.logprobs import assert_logprobs
from ...evidence import journal
from ...http.client import RecordingClient
from ...profile.schema import RESPONSES_OPTIONS, RESPONSES_REASONING_EFFORTS, Ingress, Model
from ...surfaces.base import Surface
from ..conftest import budget_for, check_completion, check_json_response

pytestmark = pytest.mark.surface("responses")


def _request(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    option: str,
    value: Any,
) -> dict[str, Any]:
    body = surface.option_body(model.public_id, budget_for(model, floor=256), option, value)
    response = check_json_response(client.post(surface.path, json_body=body), ingress)
    check_completion(surface.parse_completion(response), surface, model)
    return response


@pytest.mark.case_id("openai_responses.field_option")
@pytest.mark.parametrize("option", RESPONSES_OPTIONS)
def test_response_field_option(client, surface, model, ingress, option):
    if option not in ingress.capabilities.responses_options:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare Responses {option!r}")
    value = {"llmdv_marker": uuid.uuid4().hex} if option == "metadata" else 2
    response = _request(client, surface, model, ingress, option, value)
    if option == "metadata":
        observed = response.get("metadata")
        journal.record("response_metadata", observed, value, ok=observed == value)
        assert observed == value, "requested metadata was dropped or changed"
    else:
        entries = []
        for item in response["output"]:
            if isinstance(item, dict) and item.get("type") == "message":
                parts = item.get("content")
                if not isinstance(parts, list):
                    entries.append(None)
                    continue
                for part in parts:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        values = part.get("logprobs")
                        entries.append(values)
        assert_logprobs(entries, value)


@pytest.mark.case_id("openai_responses.reasoning_effort")
@pytest.mark.parametrize("effort", RESPONSES_REASONING_EFFORTS)
def test_response_reasoning_effort(client, surface, model, ingress, effort):
    if effort not in ingress.capabilities.responses_reasoning_efforts:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} does not declare Responses effort {effort!r}"
        )
    response = _request(client, surface, model, ingress, "reasoning", effort)
    reasoning = response.get("reasoning")
    observed = reasoning.get("effort") if isinstance(reasoning, dict) else None
    journal.record("response_reasoning_effort", observed, effort, ok=observed == effort)
    assert observed == effort, "requested reasoning effort was dropped or changed"
    if effort == "none":
        text = surface.parse_completion(response).reasoning_text
        journal.record("reasoning_disabled", len(text), "zero reasoning characters", ok=not text)
        assert not text, "reasoning_effort=none returned reasoning"
