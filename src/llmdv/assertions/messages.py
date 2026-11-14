"""Journaled checks for Anthropic token counts and the stream lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NoReturn

from ..evidence import journal
from ..http import sse
from ..surfaces.base import Completion
from . import fail


def _count(name: str, value: Any) -> int:
    if type(value) is not int or value < 0:
        fail(name, value, "a non-negative integer")
    return value


def assert_messages_token_count(body: dict[str, Any]) -> int:
    """Require a native integer count; bools and numeric strings are not token counts."""
    count = _count("input_tokens", body.get("input_tokens"))
    journal.record("input_tokens", count, "a non-negative integer", ok=True)
    return count


def _type_name(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        fail(name, value, "a non-empty type")
    return value


def _event_body(event: sse.Event) -> dict[str, Any]:
    try:
        body = event.json()
    except sse.StreamError as exc:
        fail("messages_event_json", str(exc), "a JSON object")
    if not isinstance(body, dict):
        fail(
            "messages_event_json",
            {"index": event.index, "type": type(body).__name__},
            "a JSON object",
        )
    kind = _type_name("messages_event_name", body.get("type"))
    if event.event != kind:
        fail(
            "messages_event_name",
            {"event": event.event, "type": kind},
            "matching, non-empty SSE event and JSON type",
        )
    return body


_DELTA_FIELDS = {
    "text_delta": ("text", "text"),
    "thinking_delta": ("thinking", "thinking"),
    "signature_delta": ("thinking", "signature"),
    "input_json_delta": ("tool_use", "partial_json"),
}


@dataclass
class _Lifecycle:
    started: bool = False
    stopped: bool = False
    finalizing: bool = False
    active: int | None = None
    blocks: list[str] = field(default_factory=list)
    tool_inputs: dict[int, dict] = field(default_factory=dict)
    output_tokens: int | None = None

    def message_start(self, body: dict[str, Any]) -> None:
        if self.started:
            fail("messages_event_order", body["type"], "exactly one message_start")
        message = body.get("message")
        if not isinstance(message, dict):
            fail("messages_start", message, "a Message object")
        if message.get("type") != "message":
            fail("messages_start_type", message.get("type"), "message")
        if message.get("role") != "assistant":
            fail("messages_start_role", message.get("role"), "assistant")
        if message.get("content") != []:
            fail("messages_start_content", message.get("content"), "an empty content array")
        if message.get("stop_reason") is not None:
            fail("messages_start_stop_reason", message.get("stop_reason"), "no stop reason yet")
        usage = message.get("usage")
        if not isinstance(usage, dict):
            fail("messages_start_usage", usage, "an input usage object")
        _count("messages_input_tokens", usage.get("input_tokens"))
        self.started = True

    def _block_index(self, body: dict[str, Any], *, require_open: bool = False) -> int:
        if not self.started or self.finalizing:
            fail(
                "messages_event_order",
                body["type"],
                "content between message_start and message_delta",
            )
        index = _count("messages_block_index", body.get("index"))
        if require_open and (self.active is None or index != self.active):
            fail(
                "messages_block_order",
                {"index": index, "active": self.active},
                "the currently open block",
            )
        return index

    def content_block_start(self, body: dict[str, Any]) -> None:
        index = self._block_index(body)
        if self.active is not None or index != len(self.blocks):
            fail(
                "messages_block_order",
                {"index": index, "active": self.active},
                f"next block {len(self.blocks)} with no open block",
            )
        block = body.get("content_block")
        if not isinstance(block, dict):
            fail("messages_block", block, "a content block object")
        kind = _type_name("messages_block_type", block.get("type"))
        if kind in {"text", "thinking"} and not isinstance(block.get(kind), str):
            fail("messages_block_text", block.get(kind), f"string {kind}")
        if kind == "tool_use":
            for field_name in ("id", "name"):
                _type_name(f"messages_tool_{field_name}", block.get(field_name))
            if not isinstance(block.get("input"), dict):
                fail("messages_tool_input", block.get("input"), "an input object")
            self.tool_inputs[index] = block["input"]
        self.blocks.append(kind)
        self.active = index

    def content_block_delta(self, body: dict[str, Any]) -> None:
        index = self._block_index(body, require_open=True)
        delta = body.get("delta")
        if not isinstance(delta, dict):
            fail("messages_block_delta", delta, "a delta object")
        kind = _type_name("messages_delta_type", delta.get("type"))
        if kind == "input_json_delta" and self.tool_inputs.get(index):
            fail(
                "messages_tool_initial_input",
                self.tool_inputs[index],
                "empty input when arguments arrive as JSON deltas",
            )
        if kind in _DELTA_FIELDS:
            block_type, field_name = _DELTA_FIELDS[kind]
            if self.blocks[index] != block_type:
                fail("messages_delta_block_type", self.blocks[index], block_type)
            if not isinstance(delta.get(field_name), str):
                fail("messages_delta_text", delta.get(field_name), f"string {field_name}")

    def content_block_stop(self, body: dict[str, Any]) -> None:
        self._block_index(body, require_open=True)
        self.active = None

    def message_delta(self, body: dict[str, Any]) -> None:
        if not self.started or self.active is not None:
            fail(
                "messages_event_order",
                {"event": body["type"], "active": self.active},
                "message_delta after message_start and all block stops",
            )
        delta = body.get("delta")
        if not isinstance(delta, dict):
            fail("messages_delta", delta, "a message delta object")
        reason = delta.get("stop_reason")
        if reason is not None and not isinstance(reason, str):
            fail("messages_stop_reason_type", reason, "a string or null")
        usage = body.get("usage")
        if not isinstance(usage, dict):
            fail("messages_delta_usage", usage, "an output usage object")
        if "input_tokens" in usage:
            _count("messages_input_tokens", usage["input_tokens"])
        value = _count("messages_output_tokens", usage.get("output_tokens"))
        if self.output_tokens is not None and value < self.output_tokens:
            fail("messages_cumulative_usage", value, f"at least {self.output_tokens}")
        self.output_tokens = value
        self.finalizing = True

    def message_stop(self, body: dict[str, Any]) -> None:
        if not self.finalizing or self.active is not None:
            fail(
                "messages_event_order",
                {"event": body["type"], "active": self.active},
                "message_stop after message_delta and all block stops",
            )
        self.stopped = True

    def error(self, body: dict[str, Any]) -> NoReturn:
        fail("messages_stream_error", body.get("error"), "no error event")


_HANDLERS = {
    "message_start": _Lifecycle.message_start,
    "content_block_start": _Lifecycle.content_block_start,
    "content_block_delta": _Lifecycle.content_block_delta,
    "content_block_stop": _Lifecycle.content_block_stop,
    "message_delta": _Lifecycle.message_delta,
    "message_stop": _Lifecycle.message_stop,
    "error": _Lifecycle.error,
}


def assert_messages_stream(transcript: sse.Transcript) -> None:
    """Validate the lifecycle, journaling a specific failure or one success summary.

    Pings and future event types do not change the lifecycle. Unknown fields are tolerated;
    known events must still agree with their SSE names and obey the protocol.
    """
    state = _Lifecycle()
    for event in transcript.events:
        body = _event_body(event)
        kind = body["type"]
        if state.stopped:
            fail("messages_after_stop", kind, "no events after message_stop")
        if handler := _HANDLERS.get(kind):
            handler(state, body)
    if not state.stopped:
        fail("messages_terminal", False, "a final message_stop")
    journal.record(
        "messages_lifecycle",
        {
            "events": len(transcript.events),
            "blocks": len(state.blocks),
            "output_tokens": state.output_tokens,
        },
        "well-formed message_start..message_stop",
        ok=True,
    )


def assert_stream_stop_reason(message: Completion, streamed: Completion) -> None:
    """Require the streamed and non-streamed requests to finish for the same reason."""
    observed = {"message": message.finish_reason, "stream": streamed.finish_reason}
    ok = message.finish_reason == streamed.finish_reason
    journal.record("stream_stop_reason", observed, "the same stop reason", ok=ok)
    if not ok:
        fail("stream_stop_reason", observed, "the same stop reason")
