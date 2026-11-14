"""The Response object fields a typed client requires, beyond the generation contract."""

from __future__ import annotations

from typing import Any

from ..evidence import journal
from . import fail


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _top_level(body: dict[str, Any]) -> list[str]:
    checks = {
        "id": isinstance(body.get("id"), str) and bool(body.get("id")),
        "created_at": _number(body.get("created_at")),
        "model": isinstance(body.get("model"), str),
        "object": body.get("object") == "response",
        "output": isinstance(body.get("output"), list),
        "parallel_tool_calls": isinstance(body.get("parallel_tool_calls"), bool),
        "tool_choice": isinstance(body.get("tool_choice"), (str, dict)),
        "tools": isinstance(body.get("tools"), list),
    }
    return [name for name, ok in checks.items() if not ok]


def _items(output: list[Any]) -> list[str]:
    problems, seen = [], set()
    for index, item in enumerate(output):
        if not isinstance(item, dict):
            problems.append(f"output[{index}] is not an object")
            continue
        kind, identity = item.get("type"), item.get("id")
        if kind in ("message", "reasoning") or identity is not None:
            if not (isinstance(identity, str) and identity) or identity in seen:
                problems.append(f"output[{index}].id {identity!r} is not a unique non-empty string")
            seen.add(identity)
        if kind == "message":
            parts = item.get("content") if isinstance(item.get("content"), list) else []
            for part in parts:
                if part.get("type") == "output_text" and not isinstance(
                    part.get("annotations"), list
                ):
                    problems.append(f"output[{index}] output_text has no annotations list")
        if kind == "reasoning" and not isinstance(item.get("summary"), list):
            problems.append(f"output[{index}] reasoning has no summary list")
    return problems


def _usage(usage: Any) -> list[str]:
    if not isinstance(usage, dict):
        return ["usage is not an object"]
    inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens")
    reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
    problems = []
    if not _count(cached):
        problems.append(f"input_tokens_details.cached_tokens is {cached!r}")
    if not _count(reasoning):
        problems.append(f"output_tokens_details.reasoning_tokens is {reasoning!r}")
    if _count(inputs) and _count(outputs):
        if usage.get("total_tokens") != inputs + outputs:
            problems.append("total_tokens is not input_tokens plus output_tokens")
        if _count(cached) and cached > inputs:
            problems.append("cached_tokens exceeds input_tokens")
        if _count(reasoning) and reasoning > outputs:
            problems.append("reasoning_tokens exceeds output_tokens")
    return problems


def assert_response_object(body: dict[str, Any]) -> None:
    """Required keys and types, unique item ids, part shapes and usage details."""
    missing = _top_level(body)
    if missing:
        fail("response_object_fields", missing, "every required Response field with its type")
    problems = _items(body["output"]) + _usage(body.get("usage"))
    if problems:
        fail("response_object_shape", problems, "unique item ids, part lists and usage details")
    journal.record(
        "response_object",
        {"output_items": len(body["output"]), "usage": sorted(body["usage"])},
        "required fields, unique item ids, part lists and usage details",
        ok=True,
    )
