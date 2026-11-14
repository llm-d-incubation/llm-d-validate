"""Request variants for the evolution cases, each built in its API's own shape.

A client release adds a field inside a conversation entry, a content block type, a tool type, a
header, a beta value or a new value of a known setting before any layer in the fleet knows it.
These builders express each of those once per API, so a generic case never writes a wire field
name itself. A surface that cannot express a variant has no builder for it, and the case skips
as not implemented.

Names are fixed, so two runs send the same request and their evidence compares line for line.
No real API will define a `llmdv_future` type.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import ToolSpec, WireSurface

FUTURE_FIELD = "llmdv_future_field"
FUTURE_TYPE = "llmdv_future_block"
FUTURE_TOOL = "llmdv_future_tool"
FUTURE_ITEM = "llmdv_future_item"
# A value no API defines, in the reasoning setting each API already has.
FUTURE_VALUE = "llmdv_future_value"
FUTURE_HEADER = "x-llmdv-future-header"
FUTURE_BETA = "llmdv-future-2099-01-01"

Probe = tuple[dict[str, Any], dict[str, str]]


def _future_part() -> dict[str, Any]:
    return {"type": FUTURE_TYPE, FUTURE_TYPE: {"value": "ignore me"}}


def _chat_like(body: dict[str, Any], probe: str, text_part: str) -> dict[str, Any] | None:
    """Chat and Messages keep the conversation in `messages`; they differ in parts and tools."""
    first = body["messages"][0]
    if probe == "unknown_message_field":
        first[FUTURE_FIELD] = {"value": "ignore me"}
    elif probe == "unknown_block":
        first["content"] = [{"type": "text", text_part: first["content"]}, _future_part()]
    else:
        return None
    return body


def _chat(surface, model, probe, max_tokens, prompt) -> Probe | None:
    body = surface.minimal_body(model, max_tokens, prompt)
    if probe == "unknown_enum_value":
        body["reasoning_effort"] = FUTURE_VALUE
        return body, {}
    if probe == "unknown_tool_type":
        body["tools"] = [{"type": FUTURE_TOOL, FUTURE_TOOL: {"name": "llmdv_probe"}}]
        return body, {}
    built = _chat_like(body, probe, "text")
    return (built, {}) if built is not None else None


def _messages(surface, model, probe, max_tokens, prompt) -> Probe | None:
    body = surface.minimal_body(model, max_tokens, prompt)
    if probe == "unknown_enum_value":
        body["thinking"] = {"type": FUTURE_VALUE}
        return body, {}
    if probe == "unknown_tool_type":
        # The shape of a server tool: a dated type and a name, with no input schema.
        body["tools"] = [{"type": f"{FUTURE_TOOL}_20990101", "name": "llmdv_probe"}]
        return body, {}
    if probe == "unknown_beta":
        return body, {"anthropic-beta": FUTURE_BETA}
    built = _chat_like(body, probe, "text")
    return (built, {}) if built is not None else None


def _responses(surface, model, probe, max_tokens, prompt) -> Probe | None:
    body = surface.minimal_body(model, max_tokens, prompt)
    if probe == "unknown_enum_value":
        body["reasoning"] = {"effort": FUTURE_VALUE}
        return body, {}
    if probe == "unknown_tool_type":
        body["tools"] = [{"type": FUTURE_TOOL}]
        return body, {}
    item: dict[str, Any] = {"type": "message", "role": "user", "content": prompt}
    if probe == "unknown_item":
        body["input"] = [item, {"type": FUTURE_ITEM, FUTURE_ITEM: {"value": "ignore me"}}]
        return body, {}
    if probe == "unknown_message_field":
        item[FUTURE_FIELD] = {"value": "ignore me"}
    elif probe == "unknown_block":
        item["content"] = [{"type": "input_text", "text": prompt}, _future_part()]
    else:
        return None
    body["input"] = [item]
    return body, {}


_BUILDERS: dict[str, Callable[..., Probe | None]] = {
    "chat_completions": _chat,
    "messages": _messages,
    "responses": _responses,
}


def unknown_probe(
    surface: WireSurface, model: str, probe: str, max_tokens: int, prompt: str
) -> Probe | None:
    """(body, headers) carrying one unknown element, or None where the API cannot carry it.

    An unknown header needs no builder: every API carries one on an ordinary request.
    """
    if probe == "unknown_header":
        return surface.minimal_body(model, max_tokens, prompt), {FUTURE_HEADER: "1"}
    builder = _BUILDERS.get(surface.name)
    return builder(surface, model, probe, max_tokens, prompt) if builder else None


def count_tokens_probe(surface: WireSurface, model: str, prompt: str) -> dict[str, Any] | None:
    """A count_tokens body whose user turn carries one unknown content block, Messages only."""
    if surface.name != "messages":
        return None
    return _chat_like(surface.count_tokens_body(model, prompt), "unknown_block", "text")


# One fixed conversation: a system prompt, a finished exchange and a new question. Fixed text,
# so its rendered size is a constant a profile can declare.
CANONICAL_SYSTEM = "You are a careful assistant. Answer every question with one word."
CANONICAL_TURNS = (
    ("user", "Name a primary color."),
    ("assistant", "Red."),
    ("user", "Name another primary color."),
)
CANONICAL_TOOL = ToolSpec(
    "llmdv_lookup_color",
    "Look up a color by name.",
    {
        "type": "object",
        "properties": {"name": {"type": "string"}, "shade": {"type": "integer"}},
        "required": ["name"],
    },
)


def forced_tools_body(
    surface: WireSurface,
    model: str,
    max_tokens: int,
    prompt: str,
    tools: list[ToolSpec],
) -> dict[str, Any]:
    """A tools request that names the first tool, without relying on automatic choice."""
    name = tools[0].name
    choices = {
        "chat_completions": {
            "tool_choice": {"type": "function", "function": {"name": name}},
            "parallel_tool_calls": False,
        },
        "messages": {
            "tool_choice": {"type": "tool", "name": name, "disable_parallel_tool_use": True}
        },
        "responses": {
            "tool_choice": {"type": "function", "name": name},
            "parallel_tool_calls": False,
        },
    }
    body = surface.tools_body(model, max_tokens, prompt, tools, **choices[surface.name])
    if surface.name == "messages":
        # Messages rejects a caller-selected temperature when a tool is forced.
        body.pop("temperature")
    return body


def canonical_body(
    surface: WireSurface, model: str, max_tokens: int, with_tools: bool
) -> dict[str, Any] | None:
    """The canonical conversation in this API's shape, or None where it cannot be expressed."""
    question = CANONICAL_TURNS[-1][1]
    if surface.name == "completions":
        if with_tools:
            return None
        lines = [CANONICAL_SYSTEM, *(f"{role}: {text}" for role, text in CANONICAL_TURNS)]
        return surface.minimal_body(model, max_tokens, "\n".join(lines))
    if surface.name not in _BUILDERS:
        return None
    if with_tools:
        body = forced_tools_body(surface, model, max_tokens, question, [CANONICAL_TOOL])
    else:
        body = surface.minimal_body(model, max_tokens, question)
    turns = [{"role": role, "content": text} for role, text in CANONICAL_TURNS]
    if surface.name == "chat_completions":
        body["messages"] = [{"role": "system", "content": CANONICAL_SYSTEM}, *turns]
    elif surface.name == "messages":
        body["system"] = CANONICAL_SYSTEM
        body["messages"] = turns
    else:
        body["instructions"] = CANONICAL_SYSTEM
        body["input"] = [{"type": "message", **turn} for turn in turns]
    return body


def tool_history_body(
    surface: WireSurface, model: str, max_tokens: int, value: Any
) -> dict[str, Any] | None:
    """A finished tool call whose input object carries `value`, then a new question.

    Only Messages replays a tool call's input as a JSON object; Chat and Responses carry the
    arguments as a string, which no hop re-encodes.
    """
    if surface.name != "messages":
        return None
    question = f"Call {CANONICAL_TOOL.name} once."
    body = forced_tools_body(surface, model, max_tokens, question, [CANONICAL_TOOL])
    call = {"type": "tool_use", "id": "toolu_llmdv_history", "name": CANONICAL_TOOL.name}
    body["messages"] = [
        {"role": "user", "content": "Look up the color."},
        {"role": "assistant", "content": [{**call, "input": {"name": "red", "shade": value}}]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call["id"], "content": "found"},
                {"type": "text", "text": question},
            ],
        },
    ]
    return body
