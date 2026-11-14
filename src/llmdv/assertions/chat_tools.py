"""Validate streamed function fragments before the adapter assembles them."""

from __future__ import annotations

from typing import Any

from ..evidence import journal
from . import fail


def assert_chat_tool_deltas(frames: list[dict[str, Any]]) -> None:
    ids = {}
    fragments = 0

    def check(observed: Any, expected: Any, ok: bool) -> None:
        if not ok:
            fail("chat_tool_delta", observed, expected)

    for frame in frames:
        for choice in frame["choices"]:
            parts = choice["delta"].get("tool_calls")
            if parts is None:
                continue
            check(parts, "an array of tool fragments", isinstance(parts, list))
            seen = set()
            for part in parts:
                check(part, "an object", isinstance(part, dict))
                index = part.get("index")
                check(
                    index,
                    "a distinct contiguous tool index",
                    type(index) is int and 0 <= index <= len(ids) and index not in seen,
                )
                seen.add(index)
                previous = ids.setdefault(index, None)
                if part.get("id") is not None:
                    value = part["id"]
                    check(
                        value,
                        "a stable non-empty ID",
                        isinstance(value, str)
                        and bool(value)
                        and (previous is None or previous == value),
                    )
                    ids[index] = value
                check(
                    part.get("type"), "function or absent", part.get("type") in (None, "function")
                )
                function = part.get("function")
                if function is not None:
                    check(function, "a function object", isinstance(function, dict))
                    for key in ("name", "arguments"):
                        check(
                            function.get(key),
                            "text or absent",
                            function.get(key) is None or isinstance(function[key], str),
                        )
                fragments += 1
    journal.record(
        "chat_tool_deltas",
        {"calls": len(ids), "fragments": fragments},
        "valid indexed fragments",
        ok=True,
    )
