"""Field contracts of OpenAI Chat and Completions responses and stream chunks.

Each check follows a published type in the pinned openai SDK: `chat/chat_completion.py`,
`chat/chat_completion_chunk.py`, `completion.py`, `completion_choice.py` and
`completion_usage.py`. Vendor fields are tolerated. A documented field with the wrong type or
an out-of-enum value is not, because SDK-generated types in other languages reject it.
"""

from __future__ import annotations

import time
from typing import Any

from ..evidence import journal
from . import fail

CHAT_FINISH_REASONS = frozenset({"stop", "length", "tool_calls", "content_filter", "function_call"})
TEXT_FINISH_REASONS = frozenset({"stop", "length", "content_filter"})
SERVICE_TIERS = frozenset({"auto", "default", "flex", "scale", "priority", "fast"})
# A created timestamp in milliseconds, or a clock a day away, is a unit or clock defect.
CREATED_SKEW_S = 86400
# Optional usage breakdowns in `completion_usage.py`; each is part of its total.
PROMPT_DETAILS = (
    "cached_tokens",
    "cache_write_tokens",
    "audio_tokens",
    "image_tokens",
    "text_tokens",
)
COMPLETION_DETAILS = (
    "reasoning_tokens",
    "accepted_prediction_tokens",
    "rejected_prediction_tokens",
    "audio_tokens",
    "text_tokens",
)


def _optional(value: Any, kind: type | tuple[type, ...]) -> bool:
    return value is None or (isinstance(value, kind) and not isinstance(value, bool))


def _check(name: str, ok: bool, observed: Any, expected: str) -> None:
    if not ok:
        fail(name, observed, expected)


def assert_identity_fields(body: dict[str, Any], *, now: float | None = None) -> None:
    """`id` is a non-empty string and `created` is integer seconds near the client clock."""
    now = time.time() if now is None else now
    ident = body.get("id")
    _check("response_id", isinstance(ident, str) and bool(ident), ident, "a non-empty string")
    created = body.get("created")
    in_range = type(created) is int and abs(created - now) <= CREATED_SKEW_S
    _check("created", in_range, created, f"integer Unix seconds within {CREATED_SKEW_S} s of now")
    fingerprint = body.get("system_fingerprint")
    _check(
        "system_fingerprint", _optional(fingerprint, str), fingerprint, "absent, null or a string"
    )


def assert_usage_details(usage: Any) -> None:
    """Optional usage details are integers within the totals they break down."""
    if not isinstance(usage, dict):
        fail("usage", usage, "an object")
    prompt, output = usage.get("prompt_tokens"), usage.get("completion_tokens")
    for detail_name, total, keys in (
        ("prompt_tokens_details", prompt, PROMPT_DETAILS),
        ("completion_tokens_details", output, COMPLETION_DETAILS),
    ):
        details = usage.get(detail_name)
        _check(detail_name, _optional(details, dict), details, "absent, null or an object")
        for key in keys:
            value = (details or {}).get(key)
            valid = value is None or (
                type(value) is int and type(total) is int and 0 <= value <= total
            )
            _check(key, valid, value, f"absent, null or an integer within 0..{total}")
    journal.record("usage_details", usage, "optional details within their totals", ok=True)


def assert_chat_response_fields(body: dict[str, Any], *, now: float | None = None) -> None:
    """Every documented field of a one-choice Chat response, offered no tools."""
    assert_identity_fields(body, now=now)
    tier = body.get("service_tier")
    _check(
        "service_tier",
        tier is None or tier in SERVICE_TIERS,
        tier,
        "absent, null or a documented tier",
    )
    choices = body.get("choices")
    _check("choices", isinstance(choices, list) and len(choices) == 1, choices, "one choice")
    choice = choices[0]
    index = choice.get("index")
    _check("choice_index", type(index) is int and index == 0, index, "0")
    finish = choice.get("finish_reason")
    _check(
        "finish_reason_enum",
        finish in CHAT_FINISH_REASONS,
        finish,
        f"one of {sorted(CHAT_FINISH_REASONS)}",
    )
    logprobs = choice.get("logprobs")
    _check("choice_logprobs", logprobs is None, logprobs, "absent or null when not requested")
    message = choice.get("message")
    _check("message", isinstance(message, dict), message, "an object")
    for key, kind, expected in (
        ("content", str, "a string or null"),
        ("refusal", str, "absent, null or a string"),
        ("annotations", list, "absent, null or a list"),
    ):
        _check(f"message_{key}", _optional(message.get(key), kind), message.get(key), expected)
    calls = message.get("tool_calls")
    _check("message_tool_calls", calls in (None, []), calls, "absent, null or empty without tools")
    legacy = message.get("function_call")
    _check("message_function_call", legacy is None, legacy, "absent or null without functions")
    audio = message.get("audio")
    _check("message_audio", audio is None, audio, "absent or null without the audio modality")
    metadata = body.get("metadata")
    _check("metadata", _optional(metadata, dict), metadata, "absent, null or an object")
    moderation = body.get("moderation")
    _check("moderation", moderation is None, moderation, "absent or null when not requested")
    assert_usage_details(body.get("usage"))
    journal.record(
        "chat_response_fields",
        {"id": body["id"], "created": body["created"], "finish_reason": finish},
        "documented Chat response fields",
        ok=True,
    )


def assert_completion_response_fields(
    body: dict[str, Any], choices: int = 1, *, now: float | None = None
) -> None:
    """Every documented field of a Completions response without logprobs."""
    assert_identity_fields(body, now=now)
    items = body.get("choices")
    count_ok = isinstance(items, list) and len(items) == choices
    _check("choices", count_ok, items, f"{choices} choices")
    indices = sorted(
        c.get("index") for c in items if isinstance(c, dict) and type(c.get("index")) is int
    )
    _check("choice_indices", indices == list(range(choices)), indices, f"0..{choices - 1}")
    for choice in items:
        _check("choice_text", isinstance(choice.get("text"), str), choice.get("text"), "a string")
        finish = choice.get("finish_reason")
        _check(
            "finish_reason_enum",
            finish in TEXT_FINISH_REASONS,
            finish,
            f"one of {sorted(TEXT_FINISH_REASONS)}",
        )
        logprobs = choice.get("logprobs")
        _check("choice_logprobs", logprobs is None, logprobs, "absent or null when not requested")
    assert_usage_details(body.get("usage"))
    journal.record(
        "completion_response_fields",
        {"id": body["id"], "created": body["created"], "choices": choices},
        "documented Completions response fields",
        ok=True,
    )


def _chunk_identity(frame: dict[str, Any], first: dict[str, Any], kind: str) -> None:
    _check("chunk_object", frame.get("object") == kind, frame.get("object"), kind)
    for key in ("id", "created", "model"):
        _check(
            f"chunk_{key}", frame.get(key) == first.get(key), frame.get(key), repr(first.get(key))
        )


def _usage_chunk(frame: dict[str, Any], finished: bool) -> None:
    choices = frame.get("choices")
    _check("usage_chunk_choices", choices == [], choices, "an empty choices array")
    _check("usage_after_finish", finished, finished, "usage after the finish reason")
    _check("usage_chunk_usage", isinstance(frame.get("usage"), dict), frame.get("usage"), "usage")
    assert_usage_details(frame["usage"])


def assert_chat_chunks(
    frames: list[dict[str, Any]],
    reasoning_fields: tuple[str, ...],
    include_usage: bool,
    *,
    now: float | None = None,
) -> None:
    """One-choice Chat chunks: stable identity, role first, ordered deltas, usage last."""
    if not frames:
        fail("chunks", frames, "at least one chunk")
    first = frames[0]
    assert_identity_fields(first, now=now)
    role_seen = finished = content_started = False
    usage_chunks = late_reasoning = 0
    for frame in frames:
        _chunk_identity(frame, first, "chat.completion.chunk")
        if include_usage and frame is frames[-1]:
            _usage_chunk(frame, finished)
            usage_chunks += 1
            continue
        _check("chunk_usage", frame.get("usage") is None, frame.get("usage"), "usage only last")
        choices = frame.get("choices")
        one = isinstance(choices, list) and len(choices) == 1
        _check("chunk_choices", one, choices, "one choice in every content chunk")
        choice = choices[0]
        _check("chunk_index", choice.get("index") == 0, choice.get("index"), "0")
        _check("chunk_after_finish", not finished, choice, "no choice after the finish reason")
        delta = choice.get("delta")
        _check("chunk_delta", isinstance(delta, dict), delta, "an object")
        if not role_seen:
            role = delta.get("role")
            _check("first_delta_role", role == "assistant", role, "assistant in the first delta")
            role_seen = True
        for key in ("content", "refusal", *reasoning_fields):
            _check("delta_text", _optional(delta.get(key), str), {key: delta.get(key)}, "string")
        calls = delta.get("tool_calls")
        _check("delta_tool_calls", not calls, calls, "no tool calls without tools")
        if any(delta.get(key) for key in reasoning_fields) and content_started:
            late_reasoning += 1
        content_started = content_started or bool(delta.get("content"))
        finish = choice.get("finish_reason")
        if finish is not None:
            _check(
                "finish_reason_enum",
                finish in CHAT_FINISH_REASONS,
                finish,
                f"one of {sorted(CHAT_FINISH_REASONS)}",
            )
            finished = True
    _check("reasoning_before_content", late_reasoning == 0, late_reasoning, "no late reasoning")
    _check("chunk_finish", finished, finished, "a finish reason")
    _check(
        "usage_chunks", usage_chunks == int(include_usage), usage_chunks, str(int(include_usage))
    )
    journal.record(
        "chat_chunk_fields",
        {"chunks": len(frames), "id": first["id"], "usage_chunks": usage_chunks},
        "stable identity, role first, reasoning before content, usage last",
        ok=True,
    )


def assert_completion_chunks(frames: list[dict[str, Any]], *, now: float | None = None) -> None:
    """One-choice Completions chunks with include_usage: identity, text deltas, usage last."""
    if not frames:
        fail("chunks", frames, "at least one chunk")
    first = frames[0]
    assert_identity_fields(first, now=now)
    finished = False
    for frame in frames[:-1]:
        _chunk_identity(frame, first, "text_completion")
        choices = frame.get("choices")
        one = isinstance(choices, list) and len(choices) == 1
        _check("chunk_choices", one, choices, "one choice in every text chunk")
        _check("chunk_after_finish", not finished, frame, "no text chunk after the finish reason")
        choice = choices[0]
        index = choice.get("index")
        _check("chunk_index", type(index) is int and index == 0, index, "the integer 0")
        _check("chunk_text", isinstance(choice.get("text"), str), choice.get("text"), "a string")
        finish = choice.get("finish_reason")
        if finish is not None:
            _check(
                "finish_reason_enum",
                finish in TEXT_FINISH_REASONS,
                finish,
                f"one of {sorted(TEXT_FINISH_REASONS)}",
            )
            finished = True
    _chunk_identity(frames[-1], first, "text_completion")
    _usage_chunk(frames[-1], finished)
    journal.record(
        "completion_chunk_fields",
        {"chunks": len(frames), "id": first["id"]},
        "stable identity, text deltas, usage last",
        ok=True,
    )
