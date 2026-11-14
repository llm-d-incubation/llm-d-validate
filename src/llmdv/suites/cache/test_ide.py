"""An IDE agent session on Chat that sends every part such an agent sends, together.

An IDE agent on Chat streams with usage, offers its tools with automatic choice and one call at
a time, sets its output budget with `max_completion_tokens`, and asks for a reasoning effort.
Where the profile declares the client cache hints, it also sends its cache key and options.
Only declared parts are sent.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from ...evidence import journal
from ...http.client import RecordingClient
from ...profile.schema import Ingress, Model
from ...surfaces.base import Completion, Surface
from ...surfaces.conversation import Conversation
from ..streaming.helpers import stream_generation
from .helpers import Observation, cache_config, check_cold, check_replicas, observation
from .sessions import agent_budget, check_turn, next_turn, opening


def ide_fields(
    conversation: Conversation, ingress: Ingress, model: Model, prefix_words: int
) -> dict[str, Any]:
    fields: dict[str, Any] = {"max_completion_tokens": agent_budget(model, ingress, prefix_words)}
    if conversation.tools:
        fields.update(tool_choice="auto", parallel_tool_calls=False)
    efforts = ingress.capabilities.chat_reasoning_efforts
    if efforts:
        fields["reasoning_effort"] = "high" if "high" in efforts else efforts[-1]
    return fields


def ide_turn(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    request: tuple[Conversation, dict[str, Any]],
    label: str,
) -> tuple[Observation, Completion]:
    """Stream one request with the agent's budget field and read its cache counters."""
    conversation, fields = request
    body = surface.conversation_body(model.public_id, conversation, 16, stream=True)
    # The agent sets its budget with the newer field alone.
    body.pop("max_tokens", None)
    body.update(fields)
    exchange, transcript, completion = stream_generation(client, surface, model, ingress, body)
    usage = surface.stream_cache_usage(transcript)
    config = ingress.capabilities.cache_reuse
    return observation(label, usage, exchange, config), completion


@pytest.mark.surface("chat_completions")
@pytest.mark.needs("cache_reuse")
@pytest.mark.case_id("cache.ide_session")
def test_ide_session(client, surface, model, ingress):
    """Two turns of an IDE agent session: both served, the second reads the first."""
    config = cache_config(ingress, surface)
    conversation = opening(config, model)
    hints = "cache_hints" in ingress.capabilities.scenario_checks
    if hints:
        conversation = dataclasses.replace(conversation, cache_key=f"session-{uuid.uuid4().hex}")
    fields = ide_fields(conversation, ingress, model, config.prefix_words)
    journal.record(
        "ide_parts",
        {**fields, "tools": len(conversation.tools), "cache_hints": hints},
    )
    first, reply = ide_turn(client, surface, model, ingress, (conversation, fields), "turn 1")
    check_cold(first, config)
    later = next_turn(conversation, reply, 2)
    second, _ = ide_turn(client, surface, model, ingress, (later, fields), "turn 2")
    check_replicas([first, second], config)
    check_turn(first, second, config)
