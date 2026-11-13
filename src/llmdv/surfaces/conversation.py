"""A client session in surface-neutral form, and its request body on each generation surface.

A session case builds one `Conversation` and asks its surface for the body, so the same session
runs through Chat, Messages and Responses without the case writing a wire field. A reply the
fleet returned goes back in the surface's own native form, as a client passes it back: the Chat
assistant message, the Messages content blocks, the Responses output items. A client either
keeps the reply's reasoning or strips it, and both are common.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from .base import Completion, ToolCall, ToolSpec

# Reasoning fields a Chat assistant message may carry, by engine version.
CHAT_REASONING_FIELDS = ("reasoning", "reasoning_content")
MESSAGES_REASONING_BLOCKS = ("thinking", "redacted_thinking")


@dataclass(frozen=True)
class ToolUse:
    """A tool call already in the history, with the arguments the client recorded."""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Turn:
    """One history entry.

    `role` is `user`, `system` (a system message inside the history), `assistant` (a `reply`
    the fleet returned), `assistant_text` (assistant text the client wrote), `tool_call` (an
    assistant tool call the history already holds) or `tool_result` (the client's answer to
    `call`).
    """

    role: str
    text: str = ""
    reply: Completion | None = None
    keep_reasoning: bool = True
    call: ToolUse | None = None


@dataclass(frozen=True)
class Conversation:
    """Top-level system blocks, the offered tools and the history, in order.

    `cache_key` is a client's session cache hint, sent in each surface's own form: Chat and
    Responses send it as `prompt_cache_key` with 24h retention and a 30m minimum lifetime in
    `prompt_cache_options`, Messages as a 1h ttl on its cache marker.
    """

    system: tuple[str, ...] = ()
    turns: tuple[Turn, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    cache_key: str | None = None

    def then(self, *turns: Turn) -> Conversation:
        return replace(self, turns=self.turns + turns)

    def user(self, text: str) -> Conversation:
        return self.then(Turn("user", text))

    def inline_system(self, text: str) -> Conversation:
        return self.then(Turn("system", text))

    def assistant(self, text: str) -> Conversation:
        """An assistant turn the client wrote itself, such as a replayed transcript's."""
        return self.then(Turn("assistant_text", text))

    def replied(self, reply: Completion, *, keep_reasoning: bool = True) -> Conversation:
        return self.then(Turn("assistant", reply=reply, keep_reasoning=keep_reasoning))

    def tool_exchange(self, call: ToolUse, result: str) -> Conversation:
        return self.then(Turn("tool_call", call=call), Turn("tool_result", result, call=call))

    def tool_results(self, calls: tuple[ToolCall, ...], result: str) -> Conversation:
        """The client's answer to each call a reply made, keyed by the returned call ID."""
        return self.then(
            *(Turn("tool_result", result, call=ToolUse(str(c.id), str(c.name), {})) for c in calls)
        )

    def with_system(self, *blocks: str) -> Conversation:
        return replace(self, system=tuple(blocks))

    def compacted(self, summary: str) -> Conversation:
        """The same system and tools, the history replaced by one summary turn."""
        return replace(self, turns=(Turn("user", summary),))


def _reply(turn: Turn) -> Completion:
    if turn.reply is None:
        raise ValueError("an assistant turn needs the reply the fleet returned")
    return turn.reply


def chat_messages(conversation: Conversation) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if conversation.system:
        blocks = conversation.system
        content: Any = (
            blocks[0] if len(blocks) == 1 else [{"type": "text", "text": b} for b in blocks]
        )
        messages.append({"role": "system", "content": content})
    for turn in conversation.turns:
        if turn.role in ("user", "system"):
            messages.append({"role": turn.role, "content": turn.text})
        elif turn.role == "assistant_text":
            messages.append({"role": "assistant", "content": turn.text})
        elif turn.role == "assistant":
            reply = _reply(turn)
            # The native message without its null fields, as a client serialises it.
            message = {k: v for k, v in reply.assistant_message.items() if v not in (None, [])}
            message["role"] = "assistant"
            if not turn.keep_reasoning:
                for name in CHAT_REASONING_FIELDS:
                    message.pop(name, None)
                message["content"] = reply.text
            messages.append(message)
        elif turn.role == "tool_call" and turn.call is not None:
            call = {
                "id": turn.call.call_id,
                "type": "function",
                "function": {"name": turn.call.name, "arguments": json.dumps(turn.call.arguments)},
            }
            messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif turn.role == "tool_result" and turn.call is not None:
            messages.append(
                {"role": "tool", "tool_call_id": turn.call.call_id, "content": turn.text}
            )
        else:
            raise ValueError(f"unknown turn {turn.role!r}")
    return messages


def messages_history(conversation: Conversation) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for turn in conversation.turns:
        if turn.role in ("user", "assistant_text"):
            role = "user" if turn.role == "user" else "assistant"
            history.append({"role": role, "content": [{"type": "text", "text": turn.text}]})
        elif turn.role == "system":
            # Where coding agents put reminders: a system entry inside `messages`.
            history.append({"role": "system", "content": turn.text})
        elif turn.role == "assistant":
            reply = _reply(turn)
            blocks = [b for b in reply.raw.get("content") or [] if isinstance(b, dict)]
            if not turn.keep_reasoning:
                blocks = [b for b in blocks if b.get("type") not in MESSAGES_REASONING_BLOCKS]
            # A reply that was all reasoning leaves no block; an empty text block is invalid.
            fallback = [{"type": "text", "text": reply.text or "(no answer)"}]
            history.append({"role": "assistant", "content": blocks or fallback})
        elif turn.role == "tool_call" and turn.call is not None:
            use = {
                "type": "tool_use",
                "id": turn.call.call_id,
                "name": turn.call.name,
                "input": turn.call.arguments,
            }
            history.append({"role": "assistant", "content": [use]})
        elif turn.role == "tool_result" and turn.call is not None:
            result = {"type": "tool_result", "tool_use_id": turn.call.call_id, "content": turn.text}
            history.append({"role": "user", "content": [result]})
        else:
            raise ValueError(f"unknown turn {turn.role!r}")
    return history


def messages_system(conversation: Conversation) -> list[dict[str, Any]]:
    """Block-form system, the cache marker on its last block, as coding agents send it."""
    system: list[dict[str, Any]] = [{"type": "text", "text": b} for b in conversation.system]
    if system:
        system[-1]["cache_control"] = {"type": "ephemeral"}
        if conversation.cache_key is not None:
            system[-1]["cache_control"]["ttl"] = "1h"
    return system


def openai_cache_hints(conversation: Conversation) -> dict[str, Any]:
    """The Chat and Responses cache hint fields, when the session sends them."""
    if conversation.cache_key is None:
        return {}
    return {
        "prompt_cache_key": conversation.cache_key,
        "prompt_cache_retention": "24h",
        "prompt_cache_options": {"ttl": "30m"},
    }


def responses_input(conversation: Conversation) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for turn in conversation.turns:
        if turn.role in ("user", "system"):
            items.append({"role": turn.role, "content": turn.text})
        elif turn.role == "assistant_text":
            items.append({"role": "assistant", "content": turn.text})
        elif turn.role == "assistant":
            output = [i for i in _reply(turn).raw.get("output") or [] if isinstance(i, dict)]
            if not turn.keep_reasoning:
                output = [item for item in output if item.get("type") != "reasoning"]
            items.extend(output)
        elif turn.role == "tool_call" and turn.call is not None:
            items.append(
                {
                    "type": "function_call",
                    "call_id": turn.call.call_id,
                    "name": turn.call.name,
                    "arguments": json.dumps(turn.call.arguments),
                }
            )
        elif turn.role == "tool_result" and turn.call is not None:
            items.append(
                {"type": "function_call_output", "call_id": turn.call.call_id, "output": turn.text}
            )
        else:
            raise ValueError(f"unknown turn {turn.role!r}")
    return items
