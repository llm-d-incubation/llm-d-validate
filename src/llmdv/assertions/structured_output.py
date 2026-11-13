"""Validate the answer itself, without repairing JSON or stripping Markdown fences."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from ..evidence import journal
from ..profile.schema import Model
from ..surfaces.base import Completion
from . import fail


def _reject_constant(value: str) -> None:
    raise ValueError(f"{value} is not a JSON number")


def assert_structured_output(text: str, schema: dict[str, Any]) -> None:
    """Require valid JSON matching the exact request schema; reasoning is not an answer."""
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        journal.record("structured_output_json", text, "a complete JSON value", ok=False)
        raise AssertionError(f"structured output is not valid JSON: {exc}") from exc

    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        detail = {"path": list(exc.absolute_path), "message": exc.message}
        journal.record("structured_output_schema", detail, "matches the request schema", ok=False)
        raise AssertionError(f"structured output violates the request schema: {detail}") from exc

    journal.record("structured_output_schema", value, "matches the request schema", ok=True)


def assert_structured_answer(completion: Completion, schema: dict[str, Any], model: Model) -> None:
    """The answer matches the request schema, and a reasoning model reasoned before it."""
    assert_structured_output(completion.text, schema)
    assert_structured_reasoning(completion, model)


def assert_structured_reasoning(completion: Completion, model: Model) -> None:
    """A schema constrains the answer, not the reasoning before it.

    Judged only where the profile declares reasoning on by default through a reasoning parser.
    There a schema answer with no reasoning text and no reasoning tokens means the grammar held
    from the first token and left the model no reasoning channel, so any reasoning it did went
    into the JSON's own strings: a valid object that carries the chain of thought. Reasoning
    text in any carrier counts, and so does a positive reasoning token count where the surface
    reports one. Run after the schema check, so a broken answer still fails as one.
    """
    declared = model.reasoning
    if not (declared.default_on and declared.parser):
        return
    count = completion.usage.reasoning_tokens
    observed = {
        "reasoning_chars": len(completion.reasoning_text.strip()),
        "reasoning_tokens": count,
        "output_tokens": completion.usage.completion_tokens,
    }
    expected = "reasoning text or reasoning_tokens > 0: the profile declares reasoning on"
    if not (observed["reasoning_chars"] or (type(count) is int and count > 0)):
        fail("structured_output_reasoning", observed, expected)
    journal.record("structured_output_reasoning", observed, expected, ok=True)
