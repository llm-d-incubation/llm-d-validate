"""Opt-in native thinking configuration, output effort, and invalid-effort rejection."""

import pytest

from ...assertions import assert_answer_only, assert_client_error, assert_error_envelope
from ...assertions.messages import assert_messages_stream, assert_stream_stop_reason
from ...assertions.thinking import assert_thinking_blocks
from ...evidence import journal
from ...profile.schema import MESSAGES_EFFORTS, MESSAGES_THINKING_MODES
from ...surfaces.base import REASONING_ANSWER
from ..conftest import check_basic_completion, check_completion, check_sse_response

pytestmark = pytest.mark.surface("messages")

INVALID_EFFORT = "llmdv-not-an-effort"
assert INVALID_EFFORT not in MESSAGES_EFFORTS

# Each mode at the API's minimum manual budget, plus a larger budget and cap agent clients send.
# The values are plain numbers, so they become part of the result's variant.
THINKING_CASES = [(mode, None, None) for mode in MESSAGES_THINKING_MODES] + [
    ("enabled", 2048, 4000)
]


@pytest.mark.case_id("anthropic_messages.thinking_mode")
@pytest.mark.parametrize(
    "mode,budget_tokens,max_tokens",
    THINKING_CASES,
    ids=["-".join(str(v) for v in case if v is not None) for case in THINKING_CASES],
)
def test_thinking_mode(client, surface, model, ingress, mode, budget_tokens, max_tokens):
    if mode not in ingress.capabilities.messages_thinking:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare thinking mode {mode!r}")
    shape = {"budget_tokens": budget_tokens, "max_tokens": max_tokens} if budget_tokens else {}
    body = surface.thinking_body(model.public_id, mode=mode, **shape)
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    assert_thinking_blocks(completion, mode)
    assert_answer_only(completion, model, REASONING_ANSWER)


@pytest.mark.case_id("anthropic_messages.reasoning_effort")
@pytest.mark.parametrize("effort", MESSAGES_EFFORTS)
def test_messages_effort(client, surface, model, ingress, effort):
    if effort not in ingress.capabilities.messages_efforts:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} does not declare Messages effort {effort!r}"
        )
    journal.record("requested_messages_effort", effort)
    body = surface.thinking_body(model.public_id, effort=effort)
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    assert_thinking_blocks(completion, "adaptive")


@pytest.mark.case_id("anthropic_messages.thinking_mode_streaming")
@pytest.mark.parametrize("mode", MESSAGES_THINKING_MODES)
def test_thinking_mode_streaming(client, surface, model, ingress, mode):
    if mode not in ingress.capabilities.messages_thinking:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare thinking mode {mode!r}")
    body = surface.thinking_body(model.public_id, mode=mode)
    message = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    body["stream"] = True
    exchange, transcript = client.stream(surface.path, json_body=body, stop_event="message_stop")
    check_sse_response(exchange, ingress)
    assert_messages_stream(transcript)
    completion = surface.assemble(
        [surface.parse_chunk(event.json()) for event in transcript.events]
    )
    check_completion(completion, surface, model)
    assert_stream_stop_reason(message, completion)
    assert_thinking_blocks(completion, mode)
    assert_answer_only(completion, model, REASONING_ANSWER)


@pytest.mark.case_id("anthropic_messages.reasoning_effort_streaming")
@pytest.mark.parametrize("effort", MESSAGES_EFFORTS)
def test_messages_effort_streaming(client, surface, model, ingress, effort):
    if effort not in ingress.capabilities.messages_efforts:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no Messages effort {effort!r}")
    journal.record("requested_messages_effort", effort)
    body = surface.thinking_body(model.public_id, effort=effort)
    message = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    body["stream"] = True
    exchange, transcript = client.stream(surface.path, json_body=body, stop_event="message_stop")
    check_sse_response(exchange, ingress)
    assert_messages_stream(transcript)
    completion = surface.assemble(
        [surface.parse_chunk(event.json()) for event in transcript.events]
    )
    check_completion(completion, surface, model)
    assert_stream_stop_reason(message, completion)
    assert_thinking_blocks(completion, "adaptive")


@pytest.mark.case_id("anthropic_messages.invalid_effort")
def test_invalid_effort_is_rejected(client, surface, model, ingress):
    if not ingress.capabilities.messages_efforts:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no Messages effort values")
    journal.record("sent_messages_effort", INVALID_EFFORT)
    body = surface.minimal_body(
        model.public_id, max_tokens=8, output_config={"effort": INVALID_EFFORT}
    )
    exchange = client.post(surface.path, json_body=body)

    generated = surface.looks_like_success(exchange.response_body)
    journal.record("invalid_effort_generation", generated, "no generation", ok=not generated)
    assert not generated, (
        "the layer discarded or rewrote output_config.effort instead of validating its "
        "out-of-vocabulary value"
    )

    rejected = exchange.status is not None and 400 <= exchange.status < 500
    journal.record(
        "invalid_effort_rejection_status",
        exchange.status,
        "a 4xx rejection status",
        ok=rejected,
    )
    assert rejected, (
        f"the invalid effort must receive a 4xx validation rejection, got status "
        f"{exchange.status}; redirects, successful responses, and server failures do not "
        "prove the value was validated"
    )
    assert_client_error(exchange, ingress, "invalid_field_value")
    assert_error_envelope(exchange, surface, ingress, "invalid_field_value")
