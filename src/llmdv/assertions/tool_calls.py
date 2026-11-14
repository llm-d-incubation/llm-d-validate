"""Tool-call checks for turns with any number of calls, tools and tool choices."""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from ..evidence import journal
from ..profile.schema import Model
from ..surfaces.base import Completion, ToolCall
from . import fail


def _reject_constant(value: str) -> None:
    raise ValueError(f"{value} is not a JSON number")


def call_arguments(call: ToolCall) -> Any:
    """The call's arguments as a JSON value; ValueError when they are not JSON text or an object.

    Large integers are parsed exactly, so a layer that rounds them is visible.
    """
    if isinstance(call.arguments, dict):
        return call.arguments
    if not isinstance(call.arguments, str):
        raise ValueError(f"arguments must be JSON text; got {type(call.arguments).__name__}")
    return json.loads(call.arguments, parse_constant=_reject_constant)


def assert_calls(
    completion: Completion,
    schemas: Mapping[str, dict[str, Any]],
    *,
    enforced: Collection[str],
    names: Sequence[str] | None = None,
    minimum: int = 1,
    maximum: int | None = None,
    id_pattern: str | None = None,
) -> list[tuple[ToolCall, dict[str, Any]]]:
    """Every call has a unique id, an offered name and a JSON object for arguments; the
    arguments are held to the tool's schema only for the tools in `enforced`.

    `enforced` is the set of tool names whose schema the fleet promised to pin: those sent with
    `strict: true`, or all of them when the server runs `--tool-strict-level parameter`. For
    the rest a schema mismatch is recorded as evidence, not failed: neither vLLM nor OpenAI
    constrains a non-strict tool's arguments, so `{}` for a tool that wants a city is the
    model's answer, not the fleet's defect (#270). `names`, when given, is the exact multiset
    of tool names expected. Returns each call with its parsed arguments so a case can compare
    values.
    """
    calls = completion.tool_calls
    summary = [{"id": c.id, "name": c.name, "index": c.index} for c in calls]
    bound = f"{minimum}..{maximum if maximum is not None else 'n'} calls"
    if len(calls) < minimum or (maximum is not None and len(calls) > maximum):
        fail("tool_call_count", summary, bound)
    ids = [call.id for call in calls]
    if not all(isinstance(i, str) and i.strip() for i in ids) or len(set(ids)) != len(ids):
        fail("tool_call_ids", ids, "a non-empty id per call, unique within the response")
    if id_pattern is not None and not all(re.fullmatch(id_pattern, i) for i in ids):
        fail("tool_call_id_pattern", ids, f"every id matches {id_pattern}")
    if names is not None and sorted(map(str, (c.name for c in calls))) != sorted(names):
        fail("tool_call_names", [c.name for c in calls], sorted(names))
    parsed = []
    for call in calls:
        if call.name not in schemas:
            fail("tool_call_name", call.name, f"one of {sorted(schemas)}")
        try:
            value = call_arguments(call)
        except ValueError as exc:
            fail("tool_call_arguments_json", {"name": call.name, "arguments": call.arguments}, exc)
        if not isinstance(value, dict):
            fail("tool_call_arguments_object", value, "a JSON object")
        try:
            Draft202012Validator(schemas[call.name]).validate(value)
        except ValidationError as exc:
            observed = {"name": call.name, "arguments": value, "path": list(exc.absolute_path)}
            if call.name in enforced:
                fail(
                    "tool_call_arguments_schema",
                    observed,
                    f"valid for the {call.name} schema: {exc.message}",
                )
            journal.record(
                "tool_call_arguments_unconstrained",
                observed,
                f"not held to the {call.name} schema: the tool is not strict",
                ok=True,
            )
        parsed.append((call, value))
    held = sorted(name for name in enforced if name in schemas)
    journal.record(
        "tool_calls",
        [{**entry, "arguments": value} for entry, (_, value) in zip(summary, parsed, strict=True)],
        f"{bound}, unique ids, schema-valid arguments for {held or 'no strict tools'}",
        ok=True,
    )
    return parsed


def assert_no_markup(completion: Completion, markup: Sequence[str]) -> None:
    """Raw call markup in the answer text means the parser missed or leaked a call."""
    leaked = [item for item in markup if item in completion.text]
    if leaked:
        fail("tool_markup_in_answer", {"markup": leaked, "answer": completion.text[:300]}, "none")
    journal.record("tool_markup_in_answer", "none", f"none of {list(markup)}", ok=True)


def assert_no_call_markup(completion: Completion, model: Model, *, quoted: bool = False) -> None:
    """The call markup the profile declares never reaches the answer text, whatever the case
    asked for: there it is a call the parser missed or framing it leaked.

    Every generation on a tool surface runs this, not only the cases that expect a call. A
    leak beside no call otherwise fails as a missing call, or passes a case that judges only
    the prompt. A case that asks the answer to quote the markup passes `quoted`, and the
    markup found is recorded unjudged.
    """
    markup = model.tools.markup
    if not markup:
        return
    if quoted:
        found = [item for item in markup if item in completion.text]
        journal.record(
            "tool_markup_in_answer", found, "not judged: the case asks for the markup", ok=True
        )
        return
    assert_no_markup(completion, markup)


def assert_text_without_calls(completion: Completion) -> None:
    """A turn that must not call a tool returns answer text and no call.

    Call markup in that text is judged by the shared generation contract
    (`assert_no_call_markup`), before this runs.
    """
    if completion.tool_calls:
        fail("tool_calls_absent", [c.name for c in completion.tool_calls], "no tool call")
    if not completion.text.strip():
        fail(
            "answer_text",
            {"answer_chars": 0, "reasoning_chars": len(completion.reasoning_text)},
            "non-empty answer text",
        )
    journal.record(
        "answer_without_calls", {"answer_chars": len(completion.text)}, "text, no call", ok=True
    )


def assert_answer_contains(completion: Completion, values: Sequence[str]) -> None:
    """The answer quotes every fresh value the case placed in a tool result."""
    missing = [value for value in values if value not in completion.text]
    if missing:
        fail(
            "tool_result_used",
            {"missing": missing, "answer": completion.text[:300]},
            "every result value in the answer",
        )
    journal.record("tool_result_used", list(values), "every result value in the answer", ok=True)


def assert_chat_tool_openers(frames: list[dict[str, Any]]) -> None:
    """The first fragment of each streamed Chat call carries its id and a function name.

    Streaming clients start a call when its index first appears and take the id and name from
    that fragment; a call whose name arrives later is dropped or dispatched unnamed.
    """
    openers: dict[Any, dict[str, Any]] = {}
    for frame in frames:
        for choice in frame.get("choices") or []:
            for part in (choice.get("delta") or {}).get("tool_calls") or []:
                if isinstance(part, dict):
                    openers.setdefault(part.get("index"), part)
    summary = {
        str(index): {"id": part.get("id"), "name": (part.get("function") or {}).get("name")}
        for index, part in openers.items()
    }
    for value in summary.values():
        if not (isinstance(value["id"], str) and value["id"] and value["name"]):
            fail("chat_tool_openers", summary, "an id and a function name in each first fragment")
    journal.record(
        "chat_tool_openers", summary, "an id and a function name in each first fragment", ok=True
    )
