"""Shared tool identity, schema and continuation checks for every wire surface."""

from __future__ import annotations

import uuid
from typing import Any

from ...assertions.tools import assert_tool_call
from ...evidence import journal
from ...http.client import RecordingClient
from ...profile.schema import Ingress, Model
from ...surfaces.base import Completion, Surface
from ..conftest import check_basic_completion, check_completion


def tool_schema() -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {"marker": {"type": "string", "enum": [uuid.uuid4().hex[:12]]}},
        "required": ["marker"],
        "additionalProperties": False,
    }
    return schema


def check_tool_completion(
    completion: Completion, surface: Surface, model: Model, schema: dict[str, Any]
) -> None:
    check_completion(completion, surface, model)
    expected = surface.tool_finish_reason(model)
    valid_finish = completion.finish_reason == expected
    journal.record("tool_finish", completion.finish_reason, expected, ok=valid_finish)
    assert valid_finish, (
        f"forced invocation finished with {completion.finish_reason!r}, expected {expected!r}"
    )
    assert_tool_call(completion, schema)


def check_tool_result(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    body: dict[str, Any],
    completion: Completion,
) -> None:
    result = uuid.uuid4().hex[:12]
    followup = surface.tool_result_body(body, completion, result)
    answer = check_basic_completion(
        client.post(surface.path, json_body=followup), surface, model, ingress
    )
    ok = result in answer.text and not answer.tool_calls
    journal.record(
        "tool_result_round_trip",
        {"answer": answer.text, "calls": len(answer.tool_calls)},
        result,
        ok=ok,
    )
    assert ok, "continuation ignored the fresh tool result or invoked another tool"
