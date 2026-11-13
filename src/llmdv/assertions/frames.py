"""Stream invariants a client relies on that no single frame reveals.

Each surface's own stream assertion checks its grammar and lifecycle. These check properties
across frames: the role first, reasoning before the answer, thinking blocks before text, one
finish per completion stream, nothing after the terminal event, and SSE lines a strict parser
accepts.
"""

from __future__ import annotations

from typing import Any

from ..evidence import journal
from ..http.sse import Transcript
from . import fail

SSE_FIELDS = ("data", "event", "id", "retry")
REASONING_KEYS = ("reasoning_content", "reasoning")


def _frames(transcript: Transcript) -> list[dict[str, Any]]:
    frames = []
    for event in transcript.data_events:
        value = event.json()
        if isinstance(value, dict):
            frames.append(value)
    return frames


def assert_sse_lines(transcript: Transcript) -> None:
    """Every line is blank, a comment or a known field; bytes are UTF-8; no line is cut at EOF."""
    stats = transcript.framing
    journal.record("line_terminators", {k: stats.get(k) for k in ("lf", "crlf", "cr")}, "recorded")
    if not stats.get("utf8_valid", True):
        fail("stream_utf8", False, "the raw stream decodes as strict UTF-8")
    if stats.get("partial_line_at_eof"):
        fail("partial_line_at_eof", True, "the stream ends on a line terminator")
    unknown = []
    lines = transcript.raw.split("\n")
    for line in lines:
        if not line or line.startswith(":"):
            continue
        name = line.partition(":")[0]
        if name not in SSE_FIELDS:
            unknown.append(line[:80])
    if unknown:
        fail("sse_field_lines", unknown[:5], f"only {list(SSE_FIELDS)}, comments or blank lines")
    replaced = [e.index for e in transcript.data_events if "\ufffd" in e.data]
    if replaced:
        fail("replacement_characters", replaced[:5], "no U+FFFD in any frame")
    journal.record("sse_lines", len(lines), "well formed", ok=True)


def assert_nothing_after_terminal(transcript: Transcript) -> None:
    """No frame or line follows the surface's terminal event."""
    if not transcript.terminal_seen:
        fail("terminal_event", False, "a terminal event before EOF")
    if transcript.trailing_lines:
        fail("after_terminal", transcript.trailing_lines[:5], "nothing after the terminal event")
    if transcript.framing.get("drain_error"):
        # A client that reads to EOF meets this error: the Anthropic SDK does on every stream,
        # and the OpenAI SDK does on a Responses stream, which has no [DONE].
        fail(
            "after_terminal_read",
            transcript.framing["drain_error"],
            "the response ends cleanly after the terminal event",
        )
    journal.record(
        "after_terminal",
        len(transcript.trailing),
        "nothing after the terminal event",
        ok=True,
    )


def assert_chat_order(transcript: Transcript) -> None:
    """The first choice frame carries the role, and every reasoning delta precedes the answer.

    The shared choice assertion checks ids, indices and the lifecycle. A client that renders
    reasoning apart from the answer also needs these orderings, which that assertion allows.
    """
    frames = _frames(transcript)
    first = next((frame for frame in frames if frame.get("choices")), {})
    role = ((first.get("choices") or [{}])[0].get("delta") or {}).get("role")
    if role != "assistant":
        fail("first_role", role, "the first choice frame carries role assistant")
    answer_started = False
    late: list[int] = []
    for position, frame in enumerate(frames):
        for choice in frame.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                answer_started = True
            elif answer_started and any(delta.get(key) for key in REASONING_KEYS):
                late.append(position)
    if late:
        fail("reasoning_order", late[:5], "every reasoning delta before the answer")
    journal.record("chat_order", len(frames), "role first, reasoning before the answer", ok=True)


def assert_completion_frames(transcript: Transcript, model: str | None = None) -> None:
    """One id, text_completion frames echoing the model, one finish, then [DONE].

    When `stream_options.include_usage` was requested, a choice-less usage frame follows the
    finish. Without it, the finish frame is the last JSON event. The request metadata is
    carried by ``Transcript`` so the surface assertion can validate either contract.
    """
    frames = _frames(transcript)
    if not frames:
        fail("completion_frames", 0, "at least one frame")
    errors = [frame for frame in frames if "error" in frame]
    if errors:
        fail("stream_error_frame", errors[:1], "no error event in a successful stream")
    ids = {frame.get("id") for frame in frames}
    if len(ids) != 1 or not all(isinstance(i, str) and i for i in ids):
        fail("chunk_ids", sorted(map(str, ids)), "one stable non-empty id across every frame")
    with_choices = [frame for frame in frames if frame.get("choices")]
    objects = {frame.get("object") for frame in with_choices}
    if objects != {"text_completion"}:
        fail("chunk_object", sorted(map(str, objects)), "text_completion")
    models = {frame.get("model") for frame in with_choices}
    expected_model = model if model is not None else transcript.requested_model
    if expected_model is not None and models != {expected_model}:
        fail("chunk_model", sorted(map(str, models)), expected_model)
    texts = [
        choice.get("text")
        for frame in with_choices
        for choice in frame["choices"]
        if not isinstance(choice.get("text"), str)
    ]
    if texts:
        fail("chunk_text", texts[:5], "text in every choice")
    finishes = [
        (position, choice.get("finish_reason"))
        for position, frame in enumerate(frames)
        for choice in frame.get("choices") or []
        if choice.get("finish_reason")
    ]
    if len(finishes) != 1:
        fail("finish_frames", finishes, "exactly one finish")
    after = [
        position
        for position, frame in enumerate(frames)
        if position > finishes[0][0] and any(c.get("text") for c in frame.get("choices") or [])
    ]
    if after:
        fail("after_finish", after[:5], "no text after the finish")
    choice_less = [
        position
        for position, frame in enumerate(frames)
        if not frame.get("choices") or frame.get("usage")
    ]
    expected_usage = transcript.requested_usage
    if expected_usage:
        last = frames[-1]
        if (
            choice_less != [len(frames) - 1]
            or last.get("choices")
            or not isinstance(last.get("usage"), dict)
            or not last["usage"]
        ):
            fail("usage_last", last, "a choice-less usage frame last, as include_usage asks")
    elif choice_less:
        fail("usage_without_request", choice_less, "no choice-less frame without include_usage")
    if not transcript.saw_done:
        fail("terminal_done", False, "data: [DONE]")
    journal.record(
        "completion_frames",
        {"frames": len(frames), "finish": finishes[0][1], "usage": expected_usage},
        "one id, one finish, optional usage, then [DONE]",
        ok=True,
    )


def assert_thinking_first(transcript: Transcript) -> None:
    """Where a Messages stream carries thinking and text blocks, every thinking block is first."""
    kinds = [
        (value.get("content_block") or {}).get("type")
        for value in _frames(transcript)
        if value.get("type") == "content_block_start"
    ]
    thinking = [i for i, kind in enumerate(kinds) if kind in ("thinking", "redacted_thinking")]
    text = [i for i, kind in enumerate(kinds) if kind == "text"]
    if thinking and text and max(thinking) > min(text):
        fail("block_order", kinds, "thinking blocks before text")
    journal.record("block_order", kinds, "thinking blocks before text", ok=True)
