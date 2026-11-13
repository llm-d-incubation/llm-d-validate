"""A tool invocation is output only when its identity and arguments are valid."""

from __future__ import annotations

import json
from typing import Any

from ..evidence import journal
from ..surfaces.base import TOOL_NAME, Completion, ToolCall
from .structured_output import assert_structured_output


def assert_tool_call(completion: Completion, schema: dict[str, Any]) -> ToolCall:
    calls = completion.tool_calls
    journal.record("tool_call_count", len(calls), 1, ok=len(calls) == 1)
    assert len(calls) == 1, f"expected one forced tool call, got {len(calls)}"
    call = calls[0]
    valid = isinstance(call.id, str) and bool(call.id.strip()) and call.name == TOOL_NAME
    journal.record(
        "tool_identity",
        {"id": call.id, "name": call.name, "index": call.index},
        f"non-empty ID and the forced {TOOL_NAME} name",
        ok=valid,
    )
    assert valid, "tool call has a missing ID or wrong function name"
    encoded = call.arguments if isinstance(call.arguments, str) else json.dumps(call.arguments)
    assert_structured_output(encoded, schema)
    return call


def assert_tool_calls(completion: Completion, schemas: dict[str, dict[str, Any]]) -> list[ToolCall]:
    """Several calls in one turn: one per offered tool, each with its own ID and valid input."""
    calls = list(completion.tool_calls)
    ids = [call.id for call in calls]
    names = sorted(str(call.name) for call in calls)
    valid = (
        len(calls) == len(schemas)
        and all(isinstance(i, str) and i.strip() for i in ids)
        and len(set(ids)) == len(ids)
        and names == sorted(schemas)
    )
    journal.record(
        "tool_calls_in_one_turn",
        {"count": len(calls), "names": names, "distinct_ids": len(set(map(str, ids)))},
        f"one call per tool {sorted(schemas)}, each with a distinct non-empty ID",
        ok=valid,
    )
    assert valid, f"expected one call to each of {sorted(schemas)} in one turn, got {names}"
    for call in calls:
        encoded = call.arguments if isinstance(call.arguments, str) else json.dumps(call.arguments)
        assert_structured_output(encoded, schemas[call.name])
    return calls
