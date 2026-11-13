"""The native Messages response, stop and error contracts that clients branch on.

Each check journals one summary on success, or the specific failed check before raising.
"""

from __future__ import annotations

from typing import Any

from ..evidence import journal
from ..http.client import Exchange
from ..profile.schema import Ingress
from ..surfaces.base import Completion, WireSurface
from . import fail
from .errors import assert_error_envelope, assert_not_throttled

# The published error type for each status. Any other 4xx is an invalid_request_error.
ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "billing_error",
    403: "permission_error",
    404: "not_found_error",
    409: "conflict_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    504: "timeout_error",
    529: "overloaded_error",
}

STOP_REASONS = frozenset(
    {
        "end_turn",
        "max_tokens",
        "stop_sequence",
        "tool_use",
        "pause_turn",
        "refusal",
        "model_context_window_exceeded",
    }
)


def expected_error_type(status: int) -> str:
    return ERROR_TYPES.get(status, "invalid_request_error" if status < 500 else "api_error")


def assert_error_type(exchange: Exchange, surface: WireSurface) -> None:
    """The envelope's error.type is the one the status calls for; clients branch on it."""
    envelope = surface.parse_error(exchange.response_body)
    observed = envelope.type if envelope is not None else None
    expected = expected_error_type(exchange.status or 0)
    journal.record(
        "anthropic_error_type",
        {"status": exchange.status, "type": observed},
        expected,
        ok=observed == expected,
    )
    assert observed == expected, (
        f"HTTP {exchange.status} carried error.type {observed!r}; Anthropic clients expect "
        f"{expected!r}"
    )


def assert_clean_rejection(exchange: Exchange, surface: WireSurface, ingress: Ingress) -> None:
    """A refused request: a 4xx, never a 5xx, in the native envelope with the right type."""
    assert_not_throttled(exchange, ingress)
    assert exchange.transport_error is None, f"transport failure: {exchange.transport_error}"
    status = exchange.status
    ok = status is not None and 400 <= status < 500
    journal.record("rejection_status", status, "a 4xx", ok=ok)
    assert ok, f"expected a clean 4xx refusal, got {status}"
    assert_error_envelope(exchange, surface)
    assert_error_type(exchange, surface)


def _count(name: str, value: Any, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or value < 0:
        fail(name, value, "a non-negative integer")
    return value


def _blocks(content: Any) -> list[str]:
    if not isinstance(content, list):
        fail("message_content", content, "a list of content blocks")
    kinds = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if not isinstance(kind, str) or not kind:
            fail("message_block_type", block, "a block with a string type")
        fields = {
            "text": ("text",),
            "thinking": ("thinking", "signature"),
            "redacted_thinking": ("data",),
            "tool_use": ("id", "name"),
        }.get(kind, ())
        for name in fields:
            if not isinstance(block.get(name), str):
                fail(f"message_{kind}_{name}", block.get(name), f"a string {name}")
        if kind == "tool_use" and not isinstance(block.get("input"), dict):
            fail("message_tool_use_input", block.get("input"), "an input object")
        citations = block.get("citations")
        if citations is not None and not (
            isinstance(citations, list)
            and all(isinstance(c, dict) and isinstance(c.get("type"), str) for c in citations)
        ):
            fail("message_citations", citations, "a list of typed citation objects")
        kinds.append(kind)
    return kinds


def _usage(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        fail("message_usage", usage, "a usage object")
    output = _count("usage_output_tokens", usage.get("output_tokens"))
    _count("usage_input_tokens", usage.get("input_tokens"))
    created = _count(
        "usage_cache_creation_input_tokens", usage.get("cache_creation_input_tokens"), True
    )
    _count("usage_cache_read_input_tokens", usage.get("cache_read_input_tokens"), True)
    breakdown = usage.get("cache_creation")
    if breakdown is not None:
        parts = [
            _count(f"usage_cache_creation_{ttl}", (breakdown or {}).get(ttl))
            if isinstance(breakdown, dict)
            else fail("usage_cache_creation", breakdown, "a TTL breakdown object")
            for ttl in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        ]
        if created is not None and sum(parts) != created:
            fail(
                "usage_cache_creation_sum",
                {"breakdown": parts, "cache_creation_input_tokens": created},
                "a TTL breakdown that sums to cache_creation_input_tokens",
            )
    details = usage.get("output_tokens_details")
    if details is not None:
        thinking = _count(
            "usage_thinking_tokens",
            details.get("thinking_tokens") if isinstance(details, dict) else details,
        )
        if thinking > output:
            fail(
                "usage_thinking_tokens",
                {"thinking": thinking, "output": output},
                "at most output_tokens",
            )
    return usage


def assert_message_shape(body: dict[str, Any]) -> None:
    """The native Message fields a strict client reads, beyond the shared generation checks."""
    message_id = body.get("id")
    if not isinstance(message_id, str) or not message_id:
        fail("message_id", message_id, "a non-empty string id")
    reason = body.get("stop_reason")
    if reason not in STOP_REASONS:
        fail("message_stop_reason", reason, f"one of {sorted(STOP_REASONS)}")
    if "stop_sequence" not in body:
        fail("message_stop_sequence", "missing", "a stop_sequence field, null or the sequence")
    sequence = body["stop_sequence"]
    if (reason == "stop_sequence" and not isinstance(sequence, str)) or (
        reason != "stop_sequence" and sequence is not None
    ):
        fail(
            "message_stop_sequence",
            {"stop_reason": reason, "stop_sequence": sequence},
            "a string when stop_reason is stop_sequence, otherwise null",
        )
    details = body.get("stop_details")
    if details is not None and not (
        reason == "refusal" and isinstance(details, dict) and details.get("type") == "refusal"
    ):
        fail(
            "message_stop_details",
            {"stop_reason": reason, "stop_details": details},
            "null, or a refusal object with stop_reason refusal",
        )
    kinds = _blocks(body.get("content"))
    usage = _usage(body.get("usage"))
    journal.record(
        "message_shape",
        {
            "id_prefix": message_id.split("_")[0] + "_" if "_" in message_id else message_id[:8],
            "stop_reason": reason,
            "blocks": kinds,
            "usage_fields": sorted(usage),
        },
        "a well-formed native Message",
        ok=True,
    )


def input_total(usage: Any) -> int | None:
    """All input tokens, cached or not: what one request costs regardless of cache state."""
    if not isinstance(usage, dict) or type(usage.get("input_tokens")) is not int:
        return None
    extra = [usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens")]
    return usage["input_tokens"] + sum(v for v in extra if type(v) is int)


def assert_stopped_at(completion: Completion, sequence: str, stop_sequence: Any) -> None:
    """A matched stop sequence ends the text before it and is named in the response."""
    observed = {
        "stop_reason": completion.finish_reason,
        "stop_sequence": stop_sequence,
        "text_tail": completion.text[-40:],
    }
    ok = (
        completion.finish_reason == "stop_sequence"
        and stop_sequence == sequence
        and sequence not in completion.text
    )
    journal.record(
        "stop_sequence",
        observed,
        f"stop_reason stop_sequence, stop_sequence {sequence!r}, text ending before it",
        ok=ok,
    )
    assert ok, f"the stop sequence {sequence!r} was not reported as what ended the answer"


def assert_prefill_continued(completion: Completion, marker: str, opening: str) -> None:
    """A final assistant turn is continued, so the text never restates its opening."""
    text = completion.text.lstrip()
    restarted = text.startswith((opening[:1], "`"))
    ok = marker in text and not restarted
    journal.record(
        "prefill_continued",
        {"text": completion.text[:120], "restarted": restarted},
        f"a continuation of {opening!r} carrying {marker}",
        ok=ok,
    )
    assert ok, "the reply did not continue the assistant prefill; it answered from scratch"
