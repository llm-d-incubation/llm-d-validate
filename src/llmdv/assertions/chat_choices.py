"""Validate all choice indices and stream lifecycles before assembling their output."""

from __future__ import annotations

from typing import Any

from ..evidence import journal
from ..profile.schema import Model
from . import fail


def assert_choice_indices(choices: Any, count: int) -> None:
    if not (isinstance(choices, list)):
        fail("choices", choices, "a choices array")
    indices = [c.get("index") if isinstance(c, dict) else None for c in choices]
    valid = all(type(i) is int for i in indices) and sorted(indices) == list(range(count))
    if not (valid):
        fail("choice_indices", indices, f"each index in 0..{count - 1} exactly once")
    journal.record("choice_indices", indices, f"{count} distinct choices", ok=True)


def assert_choice_stream(frames: list[dict[str, Any]], count: int, model: Model) -> None:
    finished = set()
    roles = set()
    usage_seen = False
    response_id = None
    for frame in frames:
        if not (isinstance(frame, dict)):
            fail("choice_frame", frame, "an object")
        if not (frame.get("object") == "chat.completion.chunk"):
            fail("choice_object", frame.get("object"), "chat.completion.chunk")
        if not (frame.get("model") == model):
            fail("choice_model", frame.get("model"), model)
        current = frame.get("id")
        if not (
            isinstance(current, str)
            and bool(current)
            and (response_id is None or current == response_id)
        ):
            fail("choice_id", current, "a stable non-empty id")
        response_id = current
        if not (not usage_seen):
            fail("usage_terminal", usage_seen, "no frames after usage")
        choices = frame.get("choices")
        if not (isinstance(choices, list)):
            fail("choices", choices, "an array")
        if not choices:
            usage_seen = True
            if not (isinstance(frame.get("usage"), dict) and len(finished) == count):
                fail("choice_usage", frame.get("usage"), "usage after every choice finishes")
        batch = set()
        for choice in choices:
            if not (isinstance(choice, dict)):
                fail("choice", choice, "an object")
            index = choice.get("index")
            valid = (
                type(index) is int
                and 0 <= index < count
                and index not in finished
                and index not in batch
            )
            if not (valid):
                fail("choice_index", index, "an unfinished, distinct, in-range choice")
            batch.add(index)
            delta = choice.get("delta")
            if not (isinstance(delta, dict)):
                fail("choice_delta", delta, "an object")
            for key in ("content", "reasoning_content", "reasoning"):
                if not (delta.get(key) is None or isinstance(delta[key], str)):
                    fail("choice_delta_text", delta.get(key), "text or null")
            finish = choice.get("finish_reason")
            if not (finish is None or isinstance(finish, str)):
                fail("choice_finish", finish, "text or null")
            if "role" in delta:
                if not (delta["role"] == "assistant"):
                    fail("choice_role", delta["role"], "assistant")
                roles.add(index)
            if choice.get("finish_reason") is not None:
                finished.add(index)
    valid = finished == roles == set(range(count)) and usage_seen
    if not (valid):
        fail(
            "choice_lifecycle",
            {"finished": sorted(finished), "roles": sorted(roles), "usage": usage_seen},
            "all choices completed, followed by usage",
        )
    journal.record(
        "choice_lifecycle",
        {"choices": count, "frames": len(frames)},
        "all choices completed before usage",
        ok=True,
    )
