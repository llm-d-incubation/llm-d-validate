"""A coding-CLI session on Responses that sends every part such a CLI sends, together.

A Responses-based coding CLI keeps no state on the server: each request carries the whole
history as input items, with `store` off, the reasoning it wants back encrypted, its tools with
automatic choice, and a large output budget, all streamed. Its user turns are message items
with `input_text` blocks, which a raw session case sends as plain strings. Where the profile
declares the client cache hints, the session also sends its cache key, the cache options and an
explicit breakpoint on its last user block. Only declared parts are sent.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from ...evidence import journal
from ...profile.schema import Ingress, Model
from ...surfaces.conversation import Conversation, responses_input
from .helpers import cache_config, check_cold, check_replicas
from .sessions import agent_budget, check_turn, next_turn, opening, session_turn


def cli_input(conversation: Conversation) -> list[dict[str, Any]]:
    """The input items in block form, the last user block marked when the session hints."""
    items = responses_input(conversation)
    users = [
        index
        for index, item in enumerate(items)
        if item.get("role") == "user" and isinstance(item.get("content"), str)
    ]
    blocked = []
    for index, item in enumerate(items):
        if index in users:
            block: dict[str, Any] = {"type": "input_text", "text": item["content"]}
            if conversation.cache_key is not None and index == users[-1]:
                block["prompt_cache_breakpoint"] = {"mode": "explicit"}
            item = {"type": "message", "role": "user", "content": [block]}
        blocked.append(item)
    return blocked


def cli_fields(
    conversation: Conversation, ingress: Ingress, model: Model, prefix_words: int
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "input": cli_input(conversation),
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": agent_budget(model, ingress, prefix_words),
    }
    if conversation.tools:
        fields.update(tool_choice="auto", parallel_tool_calls=False)
    efforts = ingress.capabilities.responses_reasoning_efforts
    if efforts:
        effort = "high" if "high" in efforts else efforts[-1]
        fields["reasoning"] = {"effort": effort, "summary": "auto"}
    return fields


@pytest.mark.surface("responses")
@pytest.mark.needs("cache_reuse")
@pytest.mark.case_id("cache.cli_session")
def test_cli_session(client, surface, model, ingress):
    """Two turns of a coding-CLI session: both served, the second reads the first."""
    config = cache_config(ingress, surface)
    conversation = opening(config, model)
    hints = "cache_hints" in ingress.capabilities.scenario_checks
    if hints:
        conversation = dataclasses.replace(conversation, cache_key=f"session-{uuid.uuid4().hex}")
    fields = cli_fields(conversation, ingress, model, config.prefix_words)
    journal.record(
        "cli_parts",
        {
            "store": fields["store"],
            "include": fields["include"],
            "tools": len(conversation.tools),
            "reasoning": fields.get("reasoning"),
            "max_output_tokens": fields["max_output_tokens"],
            "cache_hints": hints,
        },
    )
    first, reply = session_turn(client, surface, model, ingress, conversation, "turn 1", **fields)
    check_cold(first, config)
    # The reply's output items go back as returned, reasoning included.
    later = next_turn(conversation, reply, 2)
    second, _ = session_turn(
        client,
        surface,
        model,
        ingress,
        later,
        "turn 2",
        **cli_fields(later, ingress, model, config.prefix_words),
    )
    check_replicas([first, second], config)
    check_turn(first, second, config)
