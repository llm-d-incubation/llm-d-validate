"""Client sessions for the reuse cases: an opening, one turn at a time, and the turn rules."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ...assertions.turns import assert_turn_floor, assert_turn_ratio
from ...evidence import journal
from ...http.client import RecordingClient
from ...profile.schema import CacheReuse, Ingress, Model
from ...surfaces.base import Completion, Surface, ToolSpec
from ...surfaces.conversation import Conversation, ToolUse
from ..conftest import budget_for, check_basic_completion, nonce_document
from ..streaming.helpers import stream_generation
from .helpers import Observation, observation

PATH_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string", "description": "Workspace-relative path."}},
    "required": ["path"],
}


def session_budget(model: Model) -> int:
    """Room for a reasoning model to answer, so a reply passed back is never empty."""
    return budget_for(model, floor=256)


def agent_budget(model: Model, ingress: Ingress, prefix_words: int | None = None) -> int:
    """The large output budget agents set, where the declared window can hold that shape."""
    if model.max_model_len is None:
        return 2048
    if model.max_model_len < 4096 or (
        prefix_words is not None and prefix_words + 2048 >= model.max_model_len
    ):
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares a {model.max_model_len}-token window, "
            "too small for this agent session's prompt and 2048-token output budget"
        )
    return min(32000, max(2048, model.max_model_len // 8))


def offers_tools(model: Model) -> bool:
    # No tool choice is sent, so the engine must accept automatic choice.
    return model.tools.supported and model.tools.auto_choice


def question(paragraph: int) -> str:
    return f"Name one word from paragraph {paragraph} of the reference. Reply with that word only."


def opening(
    config: CacheReuse, model: Model, *, words: int | None = None, tools: bool = True
) -> Conversation:
    """A coding-agent session start: a nonce-led system prompt and, where declared, tools.

    The probe leads the system prompt and every tool description, so whichever a template
    renders first, nothing an earlier run cached matches the first block. With tools, the
    history already holds one tool call and its result, as an agent loop's does.
    """
    probe = uuid.uuid4().hex
    system = (
        f"Session {probe}. You are a coding agent working in a repository.",
        nonce_document(words or config.prefix_words),
    )
    conversation = Conversation(system=system)
    if not (tools and offers_tools(model)):
        journal.record("session_shape", {"tools": 0, "tool_exchange": False})
        return conversation.user(question(1))
    offered = (
        ToolSpec("read_file", f"Probe {probe}. Read one file of the repository.", PATH_SCHEMA),
        ToolSpec(
            "list_files", f"Probe {probe}. List one directory of the repository.", PATH_SCHEMA
        ),
    )
    call = ToolUse(f"call_{probe[:12]}", "read_file", {"path": "NOTES.md"})
    journal.record("session_shape", {"tools": len(offered), "tool_exchange": True})
    return (
        Conversation(system=system, tools=offered)
        .user("Read NOTES.md, then answer my questions about the reference.")
        .tool_exchange(call, "NOTES.md: the reference above is the only source.")
    )


def next_turn(
    conversation: Conversation, reply: Completion, paragraph: int, *, keep_reasoning: bool = True
) -> Conversation:
    """The reply passed back, then the tool results it asked for or the next question."""
    conversation = conversation.replied(reply, keep_reasoning=keep_reasoning)
    if reply.tool_calls:
        return conversation.tool_results(reply.tool_calls, "No such file.")
    return conversation.user(question(paragraph))


def session_turn(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    conversation: Conversation,
    label: str,
    **fields: Any,
) -> tuple[Observation, Completion]:
    """Stream the session's next request and read its cache counters as a client does.

    `fields` are top-level request fields the session sends on every turn, such as a salt.
    """
    body = surface.conversation_body(
        model.public_id, conversation, session_budget(model), stream=True
    )
    body.update(fields)
    exchange, transcript, completion = stream_generation(client, surface, model, ingress, body)
    usage = surface.stream_cache_usage(transcript)
    return observation(label, usage, exchange, ingress.capabilities.cache_reuse), completion


def observe_on(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    conversation: Conversation,
    label: str,
    fields: dict[str, Any] | None = None,
) -> Observation:
    """One non-streamed request on any surface, with that surface's own required headers."""
    body = surface.conversation_body(model.public_id, conversation, 16)
    body.update(fields or {})
    if (body.get("thinking") or {}).get("type") not in (None, "disabled"):
        # The native API refuses a caller-selected temperature with thinking on.
        body.pop("temperature", None)
    exchange = client.post(surface.path, json_body=body, headers=surface.extra_headers())
    completion = check_basic_completion(exchange, surface, model, ingress)
    usage = surface.parse_cache_usage(completion.raw)
    return observation(label, usage, exchange, ingress.capabilities.cache_reuse)


def check_turn(
    previous: Observation, current: Observation, config: CacheReuse, *, slack_blocks: int = 1
) -> None:
    """A request sharing an earlier prompt reads it, by block floor or ratio.

    `slack_blocks` is the number of whole blocks at the end of the earlier prompt the later
    one need not read: one for an extension, more where the later request replaced a short
    tail of the earlier one. The profile's `withheld_blocks` comes off on top of it.
    """
    if config.block_size is not None:
        assert_turn_floor(
            current.label,
            current.read,
            previous.total,
            config.block_size,
            slack_blocks,
            config.withheld_blocks,
        )
        return
    assert_turn_ratio(current.label, current.read, previous.total, config.min_ratio)
