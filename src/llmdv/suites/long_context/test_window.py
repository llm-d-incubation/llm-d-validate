"""Prompts and budgets near and over the declared context window.

A long prompt must be served and stay cheap to repeat, a long session must keep reading its
history, and a budget the window cannot hold must be refused naming the window: never a 5xx,
and never a generation from a silently clamped budget. An ingress that declares
`error_status.output_overflow` with a 2xx states that its engine clamps the budget instead
(vLLM since vllm-project/vllm#57035); the case then requires a generation whose usage fits the
window, and fails on a refusal.
"""

from __future__ import annotations

import pytest

from ...assertions.errors import (
    assert_client_error,
    assert_context_limit_error,
    assert_error_envelope,
    assert_not_throttled,
)
from ...evidence import journal
from ...http.client import RecordingClient
from ...profile.schema import Ingress, Model
from ...surfaces.base import Surface
from ...surfaces.conversation import Conversation
from ..cache.helpers import (
    cache_config,
    check_cold,
    check_replicas,
    check_warm,
    observe,
    observe_stream,
)
from ..cache.sessions import check_turn, question, session_turn
from ..conftest import nonce_document

# The share of the window, less the output budget, a near-limit prompt fills.
NEAR_LIMIT_SHARE = 0.9
OUTPUT_BUDGET = 16
# A near-limit request's output budget as a share of the window: large, as agents set it, so
# prompt plus budget nearly fills the window where a layer that renders with the budget fails.
NEAR_LIMIT_BUDGET_SHARE = 8
# The longest an over-window budget request may run. A clamped budget lets a model that never
# emits end-of-sequence generate until the request timeout, so the case bounds its own wait
# rather than spending the ingress's whole request timeout on one runaway generation.
OUTPUT_OVERFLOW_TIMEOUT_S = 200.0


def declared_window(model: Model, ingress: Ingress) -> int:
    limit = model.max_model_len
    journal.record("declared_max_model_len", limit)
    if not limit:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no max_model_len")
    return limit


@pytest.mark.surface_generic
@pytest.mark.needs("scenario_checks:output_overflow")
@pytest.mark.case_id("long_context.output_overflow")
def test_output_budget_over_the_window(client, surface, model, ingress):
    """A short prompt whose output budget alone fills the window is refused, naming it, or is
    served from a clamped budget where the ingress declares that."""
    limit = declared_window(model, ingress)
    body = surface.minimal_body(model.public_id, limit, surface.reply_prompt)
    exchange = client.post(surface.path, json_body=body, timeout=output_overflow_timeout(ingress))
    generated = surface.looks_like_success(exchange.response_body)
    if clamps_output_budget(ingress):
        # A declared quota answers 429 here too; that is the limit working, not a clamp failure.
        assert_not_throttled(exchange, ingress)
        journal.record(
            "output_overflow_generation",
            generated,
            "a generation from the clamped budget",
            ok=generated,
        )
        assert generated, (
            "the ingress declares the output budget is clamped, but the request was not served; "
            f"got status {exchange.status}, transport error {exchange.transport_error}"
        )
        completion = surface.parse_completion(exchange.response_body)
        usage = completion.usage
        observed = {
            "finish_reason": completion.finish_reason,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        }
        total = usage.total_tokens
        if (
            total is None
            and usage.prompt_tokens is not None
            and usage.completion_tokens is not None
        ):
            total = usage.prompt_tokens + usage.completion_tokens
        within = completion.has_output and total is not None and total <= limit
        journal.record(
            "output_overflow_clamp",
            observed,
            f"output, with usage within the {limit}-token window",
            ok=within,
        )
        assert within, (
            "a clamped budget must still produce output accounted within the window, got "
            f"{observed}"
        )
        return
    journal.record("output_overflow_generation", generated, "no generation", ok=not generated)
    assert not generated, (
        "an output budget the window cannot hold produced a completion; check for a silently "
        "clamped budget, and declare error_status.output_overflow: [200] if that is the engine's "
        "contract"
    )
    assert_client_error(exchange, ingress, "output_overflow")
    assert_error_envelope(exchange, surface, ingress, "context_overflow")
    assert_context_limit_error(exchange, surface, ingress, limit)


def output_overflow_timeout(ingress: Ingress) -> float:
    """The request timeout for an over-window budget: the ingress's own, capped."""
    return min(ingress.timeouts.request, OUTPUT_OVERFLOW_TIMEOUT_S)


def clamps_output_budget(ingress: Ingress) -> bool:
    """Whether the ingress declares that an over-window output budget is served, not refused."""
    declared = ingress.expected_error_status("output_overflow") or ()
    return any(200 <= status < 300 for status in declared)


def near_limit_idle(ingress: Ingress) -> float:
    """The longest a near-limit stream may go without an event: the ingress's request timeout,
    where that is longer than its `stream_idle`.

    The first event waits on the prefill of a near-window prompt, which the non-streamed case
    had the whole request timeout for; a cold one has taken over 130 s through a gateway, past
    the 120 s `stream_idle` profiles declare.
    """
    return max(ingress.timeouts.stream_idle, ingress.timeouts.request)


def words_for(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    tokens: int,
) -> int:
    """Words of nonce document that render to about `tokens`, from one calibration request."""
    config = ingress.capabilities.cache_reuse
    words = config.prefix_words
    body = surface.minimal_body(model.public_id, OUTPUT_BUDGET, nonce_document(words))
    calibration = observe(client, surface, model, ingress, body, "calibration")
    per_word = calibration.total / words
    journal.record("tokens_per_word", round(per_word, 4))
    return max(int(tokens / per_word), words)


def long_context(case):
    """Opt-in and disruptive: these prompts fill most of the window."""
    marks = (
        pytest.mark.surface_generic("cache_usage"),
        pytest.mark.needs("cache_reuse", "scenario_checks:long_context"),
        pytest.mark.disruptive("sends prompts that fill most of the context window"),
    )
    for mark in marks:
        case = mark(case)
    return case


@long_context
@pytest.mark.case_id("long_context.near_limit")
def test_near_limit_prompt(client, surface, model, ingress):
    """A prompt near the window, with a large output budget, is served and repeats warm.

    The budget is an eighth of the window, so prompt and budget together nearly fill it: a
    layer that renders or routes with the client's budget fails where the engine does not.
    Both requests stream, as a client allowing that budget does: a reply may legitimately
    decode for longer than any request timeout, while a stream that stops sending still fails.
    """
    config = cache_config(ingress, surface)
    limit = declared_window(model, ingress)
    budget = limit // NEAR_LIMIT_BUDGET_SHARE
    target = int((limit - budget) * NEAR_LIMIT_SHARE)
    words = words_for(client, surface, model, ingress, target)
    idle = near_limit_idle(ingress)
    journal.record(
        "near_limit_request",
        {"target_prompt_tokens": target, "max_tokens": budget, "stream_idle_s": idle},
    )
    prompt = f"{nonce_document(words)}\n\nReply with the word ok."
    body = surface.stream_body(model.public_id, budget, include_usage=True, prompt=prompt)
    first, repeat = [
        observe_stream(client, surface, model, ingress, body, label, idle_timeout=idle)
        for label in ("first", "repeat")
    ]
    journal.record("near_limit_prompt_tokens", first.total, f"<= {limit - budget}")
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)


@long_context
@pytest.mark.case_id("long_context.session")
def test_long_session(client, surface, model, ingress):
    """A session that grows to most of the window keeps reading its history every turn.

    It opens at half the window and each later turn adds an eighth, so a 256K window ends past
    190K tokens.
    """
    config = cache_config(ingress, surface)
    limit = declared_window(model, ingress)
    opening_words = words_for(client, surface, model, ingress, limit // 2)
    step_words = max(opening_words // 4, 1)
    session = Conversation(system=(nonce_document(opening_words),)).user(question(1))
    observations = []
    for turn in range(1, 4):
        seen, reply = session_turn(client, surface, model, ingress, session, f"turn {turn}")
        observations.append(seen)
        session = session.replied(reply, keep_reasoning=False).user(
            f"{nonce_document(step_words)}\n\n{question(turn + 1)}"
        )
    journal.record("session_prompt_tokens", [item.total for item in observations])
    check_replicas(observations, config)
    check_cold(observations[0], config)
    for previous, current in zip(observations, observations[1:], strict=False):
        check_turn(previous, current, config)
