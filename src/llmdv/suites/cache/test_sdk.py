"""A session through the pinned SDKs, each reply passed back as the SDK returned it.

Plain SDK clients append the reply object itself to the next request: the Chat message, the
Messages content blocks, the Responses output items. The SDK sends back every field the fleet
returned, explicit nulls included; the raw session cases drop them. The cache counters are read
from the SDK's typed usage, as such a client reads them.

Streaming clients use the SDK's stream helpers, which assemble the events into one final
object, with the parsed fields the helper adds, and pass that object back. A Responses stream
that ends `incomplete` or `failed` has no such object, so its terminal event's response is used.
"""

from __future__ import annotations

import uuid
from itertools import pairwise
from typing import Any

import pytest

from ...assertions import fail
from ...evidence import journal
from ...profile.schema import Ingress, Model
from ...surfaces.base import Surface
from ...surfaces.responses import TERMINAL_EVENTS
from ..conftest import check_completion, nonce_document, throttle_aware
from .helpers import Observation, cache_config, check_cold, check_replicas
from .sessions import check_turn, question, session_budget

pytestmark = [pytest.mark.surface_generic("cache_usage"), pytest.mark.needs("cache_reuse")]

TURNS = 3
SDK_FIXTURES = {
    "chat_completions": "openai_client",
    "responses": "openai_client",
    "messages": "anthropic_client",
}


def sdk_create(sdk: Any, surface: Surface, model: Model, system: str, history: list) -> Any:
    """One request through the SDK, as a plain client writes it; the raw response."""
    budget = session_budget(model)
    if surface.name == "chat_completions":
        return sdk.chat.completions.with_raw_response.create(
            model=model.public_id,
            messages=[{"role": "system", "content": system}, *history],
            max_tokens=budget,
            temperature=0,
        )
    if surface.name == "responses":
        return sdk.responses.with_raw_response.create(
            model=model.public_id,
            instructions=system,
            input=history,
            max_output_tokens=budget,
            temperature=0,
        )
    return sdk.messages.with_raw_response.create(
        model=model.public_id,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=history,
        max_tokens=budget,
        # The pinned SDK has no named temperature argument; it stays on the wire this way.
        extra_body={"temperature": 0},
    )


def sdk_stream(sdk: Any, surface: Surface, model: Model, system: str, history: list) -> Any:
    """One request through the SDK's stream helper, as a streaming client writes it."""
    budget = session_budget(model)
    if surface.name == "chat_completions":
        manager = sdk.chat.completions.stream(
            model=model.public_id,
            messages=[{"role": "system", "content": system}, *history],
            max_tokens=budget,
            temperature=0,
            stream_options={"include_usage": True},
        )
        final = "get_final_completion"
    elif surface.name == "responses":
        manager = sdk.responses.stream(
            model=model.public_id,
            instructions=system,
            input=history,
            max_output_tokens=budget,
            temperature=0,
        )
        final = "get_final_response"
    else:
        manager = sdk.messages.stream(
            model=model.public_id,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=history,
            max_tokens=budget,
            extra_body={"temperature": 0},
        )
        final = "get_final_message"
    with manager as stream:
        if surface.name == "responses":
            return responses_final(stream)
        return getattr(stream, final)()


def responses_final(stream: Any) -> Any:
    """The response the Responses stream ended with, read from its terminal event.

    The helper's `get_final_response()` accepts only `response.completed`; a reply that reaches
    `max_output_tokens` ends `response.incomplete`, as the API defines, and the helper raises
    the same error as for a stream with no terminal event. The terminal response is judged by
    the completion contract instead: an `incomplete` at `max_output_tokens` is a length finish.
    """
    terminal = None
    for event in stream:
        if event.type in TERMINAL_EVENTS:
            terminal = event
    kind = terminal.type if terminal is not None else None
    journal.record("sdk_stream_terminal", kind, f"one of {sorted(TERMINAL_EVENTS)}", ok=bool(kind))
    if terminal is None:
        fail("sdk_stream_terminal", None, f"one of {sorted(TERMINAL_EVENTS)}")
    if kind == "response.completed":
        # The helper's own final object, with the parsed fields it adds.
        return stream.get_final_response()
    return terminal.response


def typed_counts(name: str, usage: Any) -> tuple[Any, Any]:
    """Cached and prompt tokens from the SDK's typed usage, or None where it carries none."""
    if usage is None:
        return None, None
    if name == "chat_completions":
        details = usage.prompt_tokens_details
        return getattr(details, "cached_tokens", None), usage.prompt_tokens
    if name == "responses":
        details = usage.input_tokens_details
        return getattr(details, "cached_tokens", None), usage.input_tokens
    read = usage.cache_read_input_tokens
    if read is None:
        return None, None
    return read, usage.input_tokens + read + (usage.cache_creation_input_tokens or 0)


def passed_back(name: str, result: Any) -> list[Any]:
    """The reply as the SDK returned it, in the form a plain client appends it."""
    if name == "chat_completions":
        return [result.choices[0].message]
    if name == "responses":
        return list(result.output)
    # A reply that was all reasoning leaves no block; an empty content list is invalid.
    return [{"role": "assistant", "content": result.content or [{"type": "text", "text": "-"}]}]


def checked_counts(surface: Surface, model: Model, result: Any, label: str) -> tuple[int, int]:
    """The final object's completion contract and the cache counters in its typed usage."""
    check_completion(surface.parse_completion(result.to_dict()), surface, model)
    read, total = typed_counts(surface.name, result.usage)
    ok = type(read) is int and type(total) is int and total > 0 and 0 <= read <= total
    journal.record(
        "sdk_cache_counters",
        {"request": label, "read": read, "total": total},
        "integer counts: 0 <= read <= total, total > 0",
        ok=ok,
    )
    assert ok, f"{label}: the SDK's typed usage has invalid cache counters ({read!r}, {total!r})"
    return read, total


def sdk_turn(
    sdk: Any,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    request: tuple[str, list],
    label: str,
) -> tuple[Observation, Any]:
    system, history = request
    with throttle_aware(ingress):
        raw = sdk_create(sdk, surface, model, system, history)
    result = raw.parse()
    read, total = checked_counts(surface, model, result, label)
    header = ingress.capabilities.cache_reuse.replica_header
    replica = raw.headers.get(header) if header else None
    return Observation(label, read, total, replica), result


@pytest.mark.case_id("cache.sdk_session")
def test_sdk_session_reuse(request, surface, model, ingress):
    """Three turns through the pinned SDK; each reads the turn before it."""
    config = cache_config(ingress, surface)
    fixture = SDK_FIXTURES.get(surface.name)
    if fixture is None:
        pytest.skip(f"[not_implemented] no pinned SDK session for {surface.name}")
    sdk = request.getfixturevalue(fixture)
    system = (
        f"Session {uuid.uuid4().hex}. You are a coding agent working in a repository.\n\n"
        f"{nonce_document(config.prefix_words)}"
    )
    history: list[Any] = [{"role": "user", "content": question(1)}]
    observations: list[Observation] = []
    for turn in range(1, TURNS + 1):
        seen, result = sdk_turn(sdk, surface, model, ingress, (system, history), f"turn {turn}")
        observations.append(seen)
        history = [
            *history,
            *passed_back(surface.name, result),
            {"role": "user", "content": question(turn + 1)},
        ]
    check_replicas(observations, config)
    check_cold(observations[0], config)
    for previous, current in pairwise(observations):
        check_turn(previous, current, config)


@pytest.mark.case_id("cache.sdk_stream_session")
def test_sdk_stream_session_reuse(request, surface, model, ingress):
    """Three turns through the pinned SDK's stream helper; each reads the turn before it."""
    config = cache_config(ingress, surface)
    fixture = SDK_FIXTURES.get(surface.name)
    if fixture is None:
        pytest.skip(f"[not_implemented] no pinned SDK stream helper for {surface.name}")
    if config.replica_header:
        pytest.skip("[not_implemented] the SDK stream helpers expose no response headers")
    sdk = request.getfixturevalue(fixture)
    system = (
        f"Session {uuid.uuid4().hex}. You are a coding agent working in a repository.\n\n"
        f"{nonce_document(config.prefix_words)}"
    )
    history: list[Any] = [{"role": "user", "content": question(1)}]
    observations: list[Observation] = []
    for turn in range(1, TURNS + 1):
        label = f"turn {turn}"
        with throttle_aware(ingress):
            result = sdk_stream(sdk, surface, model, system, history)
        read, total = checked_counts(surface, model, result, label)
        observations.append(Observation(label, read, total, None))
        history = [
            *history,
            *passed_back(surface.name, result),
            {"role": "user", "content": question(turn + 1)},
        ]
    check_cold(observations[0], config)
    for previous, current in pairwise(observations):
        check_turn(previous, current, config)
