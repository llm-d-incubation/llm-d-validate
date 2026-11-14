"""Tool sets, prompts and turn checks shared by the tool-choice, definition and result cases."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from ...assertions import assert_error_envelope, assert_not_throttled
from ...assertions.tool_calls import assert_calls, assert_text_without_calls
from ...evidence import journal
from ...http.client import Exchange, RecordingClient
from ...profile.schema import Ingress, Model
from ...surfaces.base import Completion, Surface, ToolCall
from ...surfaces.tool_wire import ToolChoice, ToolDef, ToolWire, tool_wire
from ..conftest import budget_for, check_completion, check_json_response, check_sse_response

CITY = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
WEATHER = ToolDef(
    "get_weather",
    "Current weather for a city.",
    {
        "type": "object",
        "properties": {"city": {"type": "string"}, "unit": {"type": "string", "enum": ["c", "f"]}},
        "required": ["city"],
    },
)
TIME = ToolDef("get_time", "Current local time for a city.", CITY)
NEWS = ToolDef("get_news", "Top local news headline for a city.", CITY)

ASK_WEATHER = "What is the weather in Paris right now? Use the get_weather tool."
ASK_BOTH = (
    "I need both the current weather and the current local time in Tokyo. Call get_weather "
    "and get_time now, in parallel, in this one turn."
)
ASK_THREE = (
    "I need the current weather, the local time and the top news headline in Tokyo. Call "
    "get_weather, get_time and get_news now, all three in parallel, in this one turn."
)


def wire(surface: Surface) -> ToolWire:
    return tool_wire(surface)


def schemas(*tools: ToolDef) -> dict[str, dict[str, Any]]:
    return {tool.name: tool.schema for tool in tools}


def tool_budget(model: Model) -> int:
    """Output budget for a tool turn: a reasoning model thinks before it calls."""
    return budget_for(model, floor=512)


def fresh(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def call_id() -> str:
    """An id valid on every API, including the Messages tool_use_id pattern."""
    return f"call_llmdv{uuid.uuid4().hex[:12]}"


def require_auto(model: Model, ingress: Ingress) -> None:
    """Automatic choice is served only where the engine enables it, so it is declared."""
    if not model.tools.auto_choice:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no automatic tool choice")


def check_finish(completion: Completion, expected: str, name: str = "tool_turn_finish") -> None:
    ok = completion.finish_reason == expected
    journal.record(name, completion.finish_reason, expected, ok=ok)
    assert ok, f"finish_reason {completion.finish_reason!r}, expected {expected!r}"


def check_call_turn(
    completion: Completion,
    surface: Surface,
    model: Model,
    tools: Sequence[ToolDef],
    choice: ToolChoice | None,
    *,
    names: Sequence[str] | None = None,
    minimum: int = 1,
    maximum: int | None = None,
) -> list[tuple[ToolCall, dict[str, Any]]]:
    """The shared generation contract, which judges leaked call markup, then the calls and the
    finish reason."""
    shape = wire(surface)
    check_completion(completion, surface, model)
    calls = assert_calls(
        completion,
        schemas(*tools),
        enforced=enforced_schemas(model, tools),
        names=names,
        minimum=minimum,
        maximum=maximum,
        id_pattern=shape.id_pattern,
    )
    check_finish(completion, shape.call_finish(model, choice))
    return calls


def enforced_schemas(model: Model, tools: Sequence[ToolDef]) -> set[str]:
    """The tools whose argument schema this fleet promised to pin: strict ones, or every one
    when the profile declares `tools.strict_level: parameter`."""
    if model.tools.enforces_every_schema:
        return {tool.name for tool in tools}
    return {tool.name for tool in tools if tool.strict}


def check_text_turn(completion: Completion, surface: Surface, model: Model) -> None:
    check_completion(completion, surface, model)
    assert_text_without_calls(completion)
    check_finish(completion, wire(surface).text_finish(), "text_turn_finish")


def post_tools(
    client: RecordingClient, surface: Surface, ingress: Ingress, body: dict[str, Any]
) -> Completion:
    return surface.parse_completion(
        check_json_response(client.post(surface.path, json_body=body), ingress)
    )


def stream_tools(
    client: RecordingClient, surface: Surface, ingress: Ingress, body: dict[str, Any]
) -> Completion:
    """Stream a tool turn, validate the API's lifecycle and fragments, and assemble it."""
    exchange, transcript = client.stream(
        surface.path, json_body=body, stop_event=surface.stream_stop
    )
    check_sse_response(exchange, ingress)
    surface.assert_stream(transcript)
    wire(surface).assert_tool_stream(transcript)
    return surface.assemble([surface.parse_chunk(e.json()) for e in transcript.data_events])


def check_refusal(exchange: Exchange, surface: Surface, ingress: Ingress) -> None:
    """A 4xx with the surface's error envelope: what a clear refusal looks like."""
    assert_not_throttled(exchange, ingress)
    ok = exchange.transport_error is None and exchange.status is not None
    ok = ok and 400 <= exchange.status < 500
    journal.record("refusal_status", exchange.status, "a 4xx", ok=ok)
    assert ok, f"expected a 4xx refusal, got {exchange.status} {exchange.transport_error or ''}"
    assert_error_envelope(exchange, surface)


def served_or_refused(exchange: Exchange, surface: Surface, ingress: Ingress) -> bool:
    """A 200 or a clear refusal is acceptable; returns whether it was served."""
    if exchange.status == 200:
        check_json_response(exchange, ingress)
        journal.record("served_or_refused", "served", "served, or a 4xx envelope", ok=True)
        return True
    check_refusal(exchange, surface, ingress)
    journal.record("served_or_refused", "refused", "served, or a 4xx envelope", ok=True)
    return False
