"""Helpers shared by the spec-derived Chat and Completions contract cases."""

from __future__ import annotations

from typing import Any

import pytest

from ..assertions import (
    assert_healthy_finish,
    assert_usage_present_and_consistent,
    fail,
)
from ..assertions.field_disposition import assert_field_status
from ..evidence import journal
from ..http.client import RecordingClient
from ..profile.schema import CHAT_REASONING_EFFORTS, Ingress, Model
from ..surfaces import Completion, Surface
from ..surfaces.base import DEFAULT_PROMPT
from .conftest import (
    budget_for,
    check_basic_completion,
    check_call_markup,
    check_completion,
    check_identity,
    check_json_response,
    check_sse_response,
)


def require_contract(ingress: Ingress, surface: Surface) -> None:
    """Skip as undeclared unless the ingress opts this surface into the contract cases."""
    declared = ingress.capabilities.openai_contract
    if declared is True or (isinstance(declared, tuple) and surface.name in declared):
        return
    pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare openai_contract here")


def require_option(ingress: Ingress, option: str) -> None:
    if not ingress.capabilities.supports_option(option):
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare {option}")


def reasoning_off(ingress: Ingress, model: Model) -> dict[str, Any]:
    """The Chat field that turns reasoning off, or nothing for a model that does not reason.

    A declared quiet effort first, as it is the one the template answers without reasoning;
    else the effort `none`. A model with no off state, which declares neither, gets its lowest
    declared effort, recorded as reasoning that stays on, so the case still runs as a client of
    that model would send it. A reasoning model with no Chat effort declared skips as undeclared.
    """
    if not model.reasoning.default_on:
        return {}
    capabilities = ingress.capabilities
    if capabilities.chat_quiet_efforts:
        return {"reasoning_effort": capabilities.chat_quiet_efforts[0]}
    declared = capabilities.chat_reasoning_efforts
    if "none" in declared:
        return {"reasoning_effort": "none"}
    if not declared:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no quiet Chat effort and no "
            "Chat effort to send in its place"
        )
    lowest = next(effort for effort in CHAT_REASONING_EFFORTS if effort in declared)
    journal.record(
        "reasoning_stays_on",
        {"reasoning_effort": lowest},
        "no quiet effort or none declared: the lowest declared effort, still reasoning",
    )
    return {"reasoning_effort": lowest}


def check_stop_reason(completion: Completion, stop: str) -> None:
    """vLLM's `stop_reason` names the matched stop string when a stop string ends the text."""
    choice = completion.raw["choices"][0]
    observed = {"finish_reason": completion.finish_reason, "stop_reason": choice.get("stop_reason")}
    expected = {"finish_reason": "stop", "stop_reason": stop}
    journal.record("stop_reason", observed, expected, ok=observed == expected)
    if observed != expected:
        fail("stop_reason", observed, expected)


def check_generation(exchange, surface: Surface, model: Model, ingress: Ingress) -> Completion:
    """The shared contract without requiring output, for limits that may leave none.

    A one-token or seven-token reply on a reasoning model can be all reasoning, or nothing a
    parser can split; the case judges the token count or the finish.
    """
    completion = surface.parse_completion(check_json_response(exchange, ingress))
    surface.native_contract(completion.raw)
    check_identity(completion, surface, model)
    assert_healthy_finish(completion)
    check_call_markup(completion, surface, model)
    assert_usage_present_and_consistent(completion)
    return completion


def stream_frames(
    client: RecordingClient, surface: Surface, body: dict[str, Any], ingress: Ingress
) -> list[dict[str, Any]]:
    """Open an SSE generation, require its sentinel and return the parsed data frames."""
    exchange, transcript = client.stream(surface.path, json_body=body)
    check_sse_response(exchange, ingress)
    journal.record("terminal_done", transcript.saw_done, True, ok=transcript.saw_done)
    if not transcript.saw_done:
        fail("terminal_done", False, "stream ended with [DONE]")
    return [event.json() for event in transcript.data_events]


def assemble_stream(surface: Surface, frames: list[dict[str, Any]]) -> Completion:
    return surface.assemble([surface.parse_chunk(frame) for frame in frames])


def field_probe(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    name: str,
    declared: dict[str, str],
    extra: dict[str, Any],
    streamed: bool,
    prompt: str = DEFAULT_PROMPT,
) -> None:
    """Send one field probe and judge it by the ingress's declared disposition.

    A served field must still give an ordinary generation with output, so `prompt` must be one
    the model always answers on this surface.
    """
    disposition = declared.get(name)
    if disposition is None:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no disposition for {name}")
    budget = budget_for(model)
    if streamed:
        stream = surface.stream_body(model.public_id, budget, include_usage=True, prompt=prompt)
        body = {**stream, **extra}
        exchange, transcript = client.stream(surface.path, json_body=body)
    else:
        body = {**surface.minimal_body(model.public_id, budget, prompt), **extra}
        exchange, transcript = client.post(surface.path, json_body=body), None
    if not assert_field_status(exchange, surface, ingress, name, disposition):
        return
    if transcript is None:
        check_basic_completion(exchange, surface, model, ingress)
        return
    check_sse_response(exchange, ingress)
    journal.record("terminal_done", transcript.saw_done, True, ok=transcript.saw_done)
    if not transcript.saw_done:
        fail("terminal_done", False, "stream ended with [DONE]")
    surface.assert_stream(transcript)
    completion = surface.assemble(
        [surface.parse_chunk(event.json()) for event in transcript.data_events]
    )
    check_completion(completion, surface, model)
