"""The native Responses contract, checked before shared generation assertions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..evidence import journal
from ..http import sse
from ..surfaces.base import Completion
from ..surfaces.responses import (
    FUNCTION_ARGUMENTS,
    FUNCTION_CALL,
    PART_EVENTS,
    TERMINAL_EVENTS,
    TEXT_EVENTS,
)
from . import fail


def assert_response_contract(body: dict[str, Any]) -> None:
    """Check native shape, journaling a specific failure or one success summary.

    Terminal status is judged through finish_reason.
    """
    output = body.get("output")
    if not isinstance(output, list):
        fail("response_output", type(output).__name__, "an output array")
    messages = 0
    for item in output:
        if isinstance(item, dict) and item.get("type") == "message":
            if item.get("role") != "assistant":
                fail("response_message_role", item.get("role"), "assistant")
            if item.get("status") not in ("completed", "incomplete"):
                fail("response_message_status", item.get("status"), "completed or incomplete")
            messages += 1
    usage = body.get("usage")
    counts = (
        {key: usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")}
        if isinstance(usage, dict)
        else {}
    )
    if len(counts) != 3 or any(type(value) is not int or value < 0 for value in counts.values()):
        fail(
            "response_usage_counts",
            counts,
            "non-negative integer input_tokens, output_tokens and total_tokens",
        )
    journal.record(
        "response_contract",
        {"output_items": len(output), "messages": messages, "usage": counts},
        "output array, terminal assistant messages and integer token counts",
        ok=True,
    )


def _index(name: str, value: Any) -> int:
    if type(value) is not int or value < 0:
        fail(name, value, "a non-negative integer")
    return value


def _string(name: str, value: Any, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        fail(name, value, "a non-empty string" if nonempty else "a string")
    return value


def _object(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(name, type(value).__name__, "a JSON object")
    return value


@dataclass
class _Part:
    kind: str
    fragments: list[str] = field(default_factory=list)
    text_done: bool = False
    closed: bool = False


@dataclass
class _Item:
    id: str
    kind: str
    parts: dict[tuple[str, int], _Part] = field(default_factory=dict)
    closed: bool = False
    arguments: _Part | None = None
    call_id: str | None = None
    name: str | None = None


_PART_ITEMS = {
    "output_text": "message",
    "refusal": "message",
    "reasoning_text": "reasoning",
    "summary_text": "reasoning",
}


def _text_field(kind: str) -> str:
    return "refusal" if kind == "refusal" else "text"


@dataclass
class _Lifecycle:
    response: dict[str, Any] | None = None
    terminal: bool = False
    sequence: int = -1
    items: list[_Item] = field(default_factory=list)
    item_identity: bool = False

    def snapshot(self, body: dict[str, Any]) -> dict[str, Any]:
        response = _object("responses_snapshot", body.get("response"))
        if response.get("object") != "response":
            fail("responses_object", response.get("object"), "response")
        for key in ("id", "model"):
            value = _string(f"responses_{key}", response.get(key), nonempty=True)
            if self.response is not None and value != self.response[key]:
                fail(f"responses_{key}", value, f"unchanged {key}: {self.response[key]}")
        return response

    def created(self, body: dict[str, Any]) -> None:
        if self.response is not None:
            fail("responses_event_order", body["type"], "exactly one response.created")
        self.response = self.snapshot(body)
        if self.response.get("status") != "in_progress":
            fail("responses_progress_status", self.response.get("status"), "in_progress")

    def in_progress(self, body: dict[str, Any]) -> None:
        response = self.snapshot(body)
        if response.get("status") != "in_progress":
            fail("responses_progress_status", response.get("status"), "in_progress")

    def item_added(self, body: dict[str, Any]) -> None:
        index = _index("responses_output_index", body.get("output_index"))
        item = _object("responses_item", body.get("item"))
        identity = _string("responses_item_id", item.get("id"), nonempty=True)
        kind = _string("responses_item_type", item.get("type"), nonempty=True)
        if index != len(self.items) or any(previous.id == identity for previous in self.items):
            fail("responses_item_order", body, "a new item at the next output_index")
        if kind == "message" and item.get("role") != "assistant":
            fail("responses_item_role", item.get("role"), "assistant")
        state = _Item(identity, kind)
        if kind == FUNCTION_CALL:
            state.call_id = _string("responses_call_id", item.get("call_id"), nonempty=True)
            state.name = _string("responses_function_name", item.get("name"), nonempty=True)
            state.arguments = _Part(
                kind, [_string("responses_function_arguments", item.get("arguments"))]
            )
        self.items.append(state)

    def item(self, body: dict[str, Any], *, closing: bool = False) -> _Item:
        index = _index("responses_output_index", body.get("output_index"))
        if index >= len(self.items) or self.items[index].closed:
            fail("responses_item_order", index, "an open output item")
        item = self.items[index]
        identity = (
            _object("responses_item", body.get("item")).get("id")
            if closing
            else body.get("item_id")
        )
        if identity != item.id:
            fail("responses_item_id", identity, item.id)
        return item

    def item_done(self, body: dict[str, Any]) -> None:
        item = self.item(body, closing=True)
        if any(not part.closed for part in item.parts.values()):
            fail("responses_part_order", body["type"], "all item parts closed")
        done = body["item"]
        if done.get("type") != item.kind:
            fail("responses_item_type", done.get("type"), item.kind)
        if item.kind == "message":
            if done.get("role") != "assistant":
                fail("responses_item_role", done.get("role"), "assistant")
            if done.get("status") not in ("completed", "incomplete"):
                fail("responses_item_status", done.get("status"), "completed or incomplete")
        if item.kind == FUNCTION_CALL:
            if item.arguments is None or not item.arguments.text_done:
                fail("responses_arguments_order", done, "arguments.done before output_item.done")
            self.check_function_snapshot(item, done)
        for (bucket, index), part in item.parts.items():
            if part.kind not in _PART_ITEMS:
                continue
            parts = done.get(bucket)
            if not isinstance(parts, list) or index >= len(parts):
                fail("responses_item_content", parts, "the streamed parts in the completed item")
            raw = _object("responses_item_content", parts[index])
            if raw.get("type") != part.kind or raw.get(_text_field(part.kind)) != "".join(
                part.fragments
            ):
                fail("responses_item_content", raw, "the streamed part type and text")
        item.closed = True

    def check_function_snapshot(self, item, raw):
        expected = {
            "id": item.id,
            "type": FUNCTION_CALL,
            "name": item.name,
            "call_id": item.call_id,
            "arguments": "".join(item.arguments.fragments),
        }
        if any(raw.get(key) != value for key, value in expected.items()):
            fail("responses_function_snapshot", raw, str(expected))
        if raw.get("status") not in (None, "completed", "incomplete"):
            fail("responses_function_status", raw.get("status"), "completed or incomplete")

    def function_arguments(self, body):
        item = self.item(body)
        if item.kind != FUNCTION_CALL or item.arguments is None or item.arguments.text_done:
            fail("responses_arguments_order", body, "an unfinished function call")
        return item.arguments

    def arguments_delta(self, body):
        self.function_arguments(body).fragments.append(
            _string("responses_arguments_delta", body.get("delta"))
        )

    def arguments_done(self, body):
        part = self.function_arguments(body)
        value = _string("responses_arguments_done", body.get("arguments"))
        if value != "".join(part.fragments):
            fail("responses_arguments_consistency", value, "concatenated argument fragments")
        part.text_done = True

    def part_key(self, body: dict[str, Any]) -> tuple[str, int]:
        bucket = "summary" if body["type"].startswith("response.reasoning_summary_") else "content"
        return bucket, _index("responses_part_index", body.get(f"{bucket}_index"))

    def part_added(self, body: dict[str, Any]) -> None:
        item = self.item(body)
        key = self.part_key(body)
        if key[1] != sum(bucket == key[0] for bucket, _ in item.parts):
            fail("responses_part_order", key, "a new part at the next index")
        raw = _object("responses_part", body.get("part"))
        kind = _string("responses_part_type", raw.get("type"), nonempty=True)
        expected_item = _PART_ITEMS.get(kind)
        if expected_item is not None and item.kind != expected_item:
            fail("responses_part_type", kind, f"a part belonging to {item.kind}")
        if kind in _PART_ITEMS:
            initial = _string("responses_part_text", raw.get(_text_field(kind)))
        else:
            initial = ""  # Unrecognized part types are extensions, not generated text.
        item.parts[key] = _Part(kind, [initial])

    def part(self, body: dict[str, Any]) -> _Part:
        item = self.item(body)
        key = self.part_key(body)
        part = item.parts.get(key)
        if part is None or part.closed:
            fail("responses_part_order", key, "an open content or summary part")
        return part

    def text_part(self, body: dict[str, Any]) -> _Part:
        part = self.part(body)
        kind = TEXT_EVENTS[body["type"].rsplit(".", 1)[0]]
        if part.kind != kind or part.text_done:
            fail("responses_text_order", body["type"], f"an unfinished {part.kind} part")
        return part

    def delta(self, body: dict[str, Any]) -> None:
        part = self.text_part(body)
        part.fragments.append(_string("responses_delta", body.get("delta")))

    def text_done(self, body: dict[str, Any]) -> None:
        part = self.text_part(body)
        text = _string("responses_done_text", body.get(_text_field(part.kind)))
        if text != "".join(part.fragments):
            fail("responses_text_consistency", text, "the concatenated streamed fragments")
        part.text_done = True

    def part_done(self, body: dict[str, Any]) -> None:
        part = self.part(body)
        raw = _object("responses_part", body.get("part"))
        if raw.get("type") != part.kind:
            fail("responses_part_type", raw.get("type"), part.kind)
        if part.kind in _PART_ITEMS:
            if not part.text_done:
                fail("responses_text_order", body["type"], "text.done before part.done")
            if raw.get(_text_field(part.kind)) != "".join(part.fragments):
                fail("responses_text_consistency", raw, "the concatenated streamed fragments")
        part.closed = True

    def finished(self, body: dict[str, Any]) -> None:
        response = self.snapshot(body)
        if body["type"] == "response.incomplete" and response.get("status") != "incomplete":
            fail("responses_terminal_status", response.get("status"), "incomplete")
        if any(not item.closed for item in self.items):
            fail("responses_item_order", body["type"], "all output items closed")
        output = response.get("output")
        if self.item_identity:
            self.check_snapshot_items(output)
        for index, item in enumerate(self.items):
            if item.kind == FUNCTION_CALL:
                if not isinstance(output, list) or index >= len(output):
                    fail("responses_function_snapshot", output, "the streamed function item")
                self.check_function_snapshot(
                    item, _object("responses_function_snapshot", output[index])
                )
        # vLLM also emits response.completed for native status=incomplete. The native
        # finish_reason mapping, shared with non-streaming, decides whether it is healthy.
        self.terminal = True

    def check_snapshot_items(self, output: Any) -> None:
        """The terminal snapshot lists the streamed items, in order, under the same ids.

        A client pairs streamed items with the final response by id, and replays either one
        as the next turn's input, so an id that changes between them breaks that pairing.
        """
        streamed = [(item.id, item.kind) for item in self.items]
        final = [
            (entry.get("id"), entry.get("type")) if isinstance(entry, dict) else (None, None)
            for entry in (output if isinstance(output, list) else [])
        ]
        if final != streamed:
            fail("responses_item_identity", final, f"the streamed items {streamed}")

    def error(self, body: dict[str, Any]) -> None:
        fail("responses_stream_error", body, "no error or failed event inside HTTP 200")


_HANDLERS = {
    f"{FUNCTION_ARGUMENTS}.delta": _Lifecycle.arguments_delta,
    f"{FUNCTION_ARGUMENTS}.done": _Lifecycle.arguments_done,
    "response.created": _Lifecycle.created,
    "response.in_progress": _Lifecycle.in_progress,
    "response.output_item.added": _Lifecycle.item_added,
    "response.output_item.done": _Lifecycle.item_done,
    "response.completed": _Lifecycle.finished,
    "response.incomplete": _Lifecycle.finished,
    **{f"{name}.delta": _Lifecycle.delta for name in TEXT_EVENTS},
    **{f"{name}.done": _Lifecycle.text_done for name in TEXT_EVENTS},
    **{f"{name}.added": _Lifecycle.part_added for name in PART_EVENTS},
    **{f"{name}.done": _Lifecycle.part_done for name in PART_EVENTS},
}


def assert_responses_stream(transcript: sse.Transcript, *, item_identity: bool = False) -> None:
    """Check lifecycle and text reconstruction; journal failures or one compact summary.

    `item_identity` also requires the terminal snapshot to keep the streamed item ids. Only the
    stream contract cases ask for it, so one id defect does not fail every streamed case.
    """
    state = _Lifecycle(item_identity=item_identity)
    for event in transcript.data_events:
        try:
            body = event.json()
        except sse.StreamError as exc:
            fail("responses_event_json", str(exc), "a JSON object")
        body = _object("responses_event_json", body)
        kind = _string("responses_event_type", body.get("type"), nonempty=True)
        if event.event != kind:
            fail("responses_event_name", event.event, kind)
        if state.terminal:
            fail("responses_event_order", kind, "no events after the terminal response")
        # Errors fail before sequence validation; they may carry no sequence_number.
        if kind in ("error", "response.failed"):
            state.error(body)
        handler = _HANDLERS.get(kind)
        if handler is None:
            continue
        sequence = _index("responses_sequence", body.get("sequence_number"))
        if sequence <= state.sequence:
            fail("responses_sequence", sequence, f"greater than {state.sequence}")
        state.sequence = sequence
        if state.response is None and kind != "response.created":
            fail("responses_event_order", kind, "response.created before other response events")
        handler(state, body)
    if not state.terminal:
        fail(
            "responses_terminal",
            len(transcript.events),
            f"a terminal event: {sorted(TERMINAL_EVENTS)}",
        )
    journal.record(
        "responses_lifecycle",
        {"events": len(transcript.data_events), "items": len(state.items)},
        "well-formed response.created..terminal response",
        ok=True,
    )


def assert_response_stream_output(completion: Completion, snapshot: Completion) -> None:
    """A complete final snapshot must not hide deltas dropped by an intermediary."""
    for name in ("text", "reasoning_text"):
        if getattr(completion, name) != getattr(snapshot, name):
            fail(
                "responses_output_consistency",
                name,
                "streamed output matching the terminal snapshot",
            )

    def calls(completion):
        return [(c.id, c.name, c.arguments, c.index) for c in completion.tool_calls]

    if calls(completion) != calls(snapshot):
        fail("responses_tool_consistency", calls(completion), str(calls(snapshot)))
    journal.record(
        "responses_output_consistency", True, "streamed output matches terminal snapshot", ok=True
    )
