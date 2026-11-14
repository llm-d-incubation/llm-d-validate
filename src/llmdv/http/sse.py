"""Server-sent events: the line protocol, and a transcript of what actually arrived.

Written to be strict about the things a client contract depends on and tolerant about
everything else. A chunk from vLLM carries fields no OpenAI client knows about
(`prompt_token_ids`, `token_ids`), and a gateway may add its own; treating those as errors
would fail a working stack. What is NOT tolerated is a missing terminal sentinel, a stream
that stalls, or a frame that is not parseable at all -- those are the contract.

`[DONE]` is modelled as a sentinel rather than as data. It carries no JSON and a reader that
tries to parse it produces a confusing error at the one point in the stream where the message
should be clearest.
"""

from __future__ import annotations

import codecs
import json
import time
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

DONE = "[DONE]"

# Default cap on a transcript stored as evidence. A short completion is a few kilobytes, but a
# long agentic stream is megabytes, and a failing run writes one per failure. Configurable via
# --transcript-cap; 0 means keep everything. See --artifact-sink for offloading them whole.
DEFAULT_TRANSCRIPT_CAP = 256 * 1024


class StreamError(Exception):
    """The stream broke a rule a client depends on."""


class StreamIdleTimeout(StreamError):
    """No event arrived within the profile's stream_idle budget.

    Distinct from a request timeout: the connection is alive and the server simply stopped
    producing, which is the signature of a stalled decode rather than a slow one.
    """


@dataclass
class Event:
    """One `data:` frame."""

    data: str
    index: int
    elapsed_s: float = 0.0
    event: str | None = None

    @property
    def is_done(self) -> bool:
        return self.data.strip() == DONE

    def json(self) -> Any:
        """Parsed payload. Raises StreamError rather than ValueError so callers catch one type."""
        try:
            return json.loads(self.data)
        except ValueError as exc:
            raise StreamError(f"event {self.index} is not valid JSON: {exc}") from exc


@dataclass
class Transcript:
    """Everything that arrived, in order, for the evidence bundle.

    Kept as raw text rather than re-serialised from parsed events: evidence should show what
    the server sent, not llmdv's idea of it.
    """

    raw: str = ""
    events: list[Event] = field(default_factory=list)
    saw_done: bool = False
    bytes_received: int = 0
    client_closed: bool = False
    # Filled only when a reader asks for them: frames and non-blank lines that arrived after the
    # terminal event, and byte-level framing observations from `split_lines`.
    trailing: list[Event] = field(default_factory=list)
    trailing_lines: list[str] = field(default_factory=list)
    framing: dict[str, Any] = field(default_factory=dict)
    terminal_seen: bool = False
    requested_model: str | None = None
    requested_usage: bool = False

    @property
    def data_events(self) -> list[Event]:
        """Events excluding the terminal sentinel."""
        return [e for e in self.events if not e.is_done]


def parse_lines(lines: Iterable[str], idle_timeout: float | None = None) -> Iterator[Event]:
    """Yield one Event per `data:` frame.

    Blank lines separate frames in the SSE grammar; multi-line `data:` within one frame is
    joined with newlines, per the spec. Comment lines (`:` prefixed, used as keepalives) reset
    the idle clock without producing an event -- counting a keepalive as progress is the whole
    reason servers send them.
    """
    buffer: list[str] = []
    event_name: str | None = None
    index = 0
    started = time.monotonic()
    last = started

    for line in lines:
        now = time.monotonic()
        if idle_timeout is not None and (now - last) > idle_timeout:
            raise StreamIdleTimeout(
                f"no event for {now - last:.1f}s (limit {idle_timeout:.0f}s) after {index} event(s)"
            )
        last = now
        line = line.rstrip("\n").rstrip("\r")

        if line.startswith(":"):
            continue  # keepalive comment
        if line == "":
            if buffer:
                yield Event(
                    data="\n".join(buffer), index=index, elapsed_s=now - started, event=event_name
                )
                index += 1
                buffer = []
            event_name = None
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            buffer.append(value)
        elif field == "event":
            event_name = value or None
        # `id:`/`retry:` are irrelevant to these single-request streams.
    # EOF does not dispatch a pending frame: only a blank line completes an SSE event.


def split_lines(chunks: Iterable[bytes], stats: dict[str, Any]) -> Iterator[str]:
    """Split raw bytes into SSE lines, recording what a decoded line reader would hide.

    The spec accepts CRLF, LF and CR as line terminators; `stats` counts each, notes whether
    the bytes were valid UTF-8, and whether the stream ended in the middle of a line. A CR at
    the end of one chunk followed by LF at the start of the next is one terminator.
    """
    stats.update(crlf=0, lf=0, cr=0, utf8_valid=True, partial_line_at_eof=False)
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    pending = ""
    pending_cr = False

    def decode(data: bytes, final: bool = False) -> str:
        nonlocal decoder
        try:
            return decoder.decode(data, final)
        except UnicodeDecodeError:
            stats["utf8_valid"] = False
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            return data.decode("utf-8", "replace")

    for chunk in chunks:
        text = decode(chunk)
        index = 0
        if pending_cr and text:
            pending_cr = False
            if text.startswith("\n"):
                stats["crlf"] += 1
                index = 1
            else:
                stats["cr"] += 1
        while index < len(text):
            cr, lf = text.find("\r", index), text.find("\n", index)
            ends = [position for position in (cr, lf) if position >= 0]
            if not ends:
                pending += text[index:]
                break
            end = min(ends)
            line, pending = pending + text[index:end], ""
            if text[end] == "\n":
                stats["lf"] += 1
                index = end + 1
            elif end + 1 < len(text):
                if text[end + 1] == "\n":
                    stats["crlf"] += 1
                    index = end + 2
                else:
                    stats["cr"] += 1
                    index = end + 1
            else:
                pending_cr = True
                index = end + 1
            yield line
    pending += decode(b"", final=True)
    if pending_cr:
        stats["cr"] += 1
    if pending:
        stats["partial_line_at_eof"] = True
        yield pending


def read(
    lines: Iterable[str],
    idle_timeout: float | None = None,
    *,
    stop_event: str | Collection[str] | None = None,
    transcript: Transcript | None = None,
    stop_when: Callable[[Event], bool] | None = None,
    max_events: int | None = None,
    drain: bool = False,
    drain_limit: int = 256,
) -> Transcript:
    """Read through `[DONE]` or a surface's named terminal event, without waiting for EOF.

    Preserve received lines, including names and incomplete frames, even if reading raises.
    A caller supplying a transcript can retain that partial evidence on transport failure.
    With `drain`, reading continues after the terminal event until EOF or `drain_limit`
    lines, and whatever arrived is kept apart in `trailing` and `trailing_lines`.
    """
    transcript = transcript if transcript is not None else Transcript()
    collected: list[str] = []
    stop_events = {stop_event} if isinstance(stop_event, str) else (stop_event or ())

    terminal_at: int | None = None

    def recorded_lines():
        source = iter(lines)
        while True:
            if drain and terminal_at is not None and len(collected) - terminal_at >= drain_limit:
                transcript.framing["drain_error"] = (
                    f"drain stopped after {drain_limit} lines without observing EOF"
                )
                return
            try:
                line = next(source)
            except StopIteration:
                return
            collected.append(line)
            yield line

    try:
        events = parse_lines(recorded_lines(), idle_timeout=idle_timeout)
        for event in events:
            transcript.events.append(event)
            if event.is_done:
                transcript.saw_done = True
                terminal_at = len(collected)
                break
            if event.event in stop_events:
                terminal_at = len(collected)
                break
            if stop_when is not None and stop_when(event):
                transcript.client_closed = True
                break
            if max_events is not None and len(transcript.events) >= max_events:
                raise StreamError(
                    f"stream exceeded {max_events} events before the requested close point"
                )
        if drain and terminal_at is not None:
            for event in events:
                transcript.trailing.append(event)
    finally:
        transcript.terminal_seen = terminal_at is not None
        if terminal_at is not None:
            transcript.trailing_lines = [line for line in collected[terminal_at:] if line.strip()]
        transcript.raw = "".join(f"{line}\n" for line in collected)
        transcript.bytes_received = len(transcript.raw.encode("utf-8"))
    return transcript


def clamp(text: str, cap: int = DEFAULT_TRANSCRIPT_CAP) -> tuple[str, bool]:
    """Limit a transcript to `cap` bytes, dropping from the MIDDLE.

    The head carries the opening frames and the tail carries the finish reason, the terminal
    usage and `[DONE]` -- the two ends are what a reader needs, and truncating from the end
    would throw away the half that decides the verdict.

    `cap <= 0` keeps everything. Returns (text, was_truncated) so the caller can record that
    the evidence is partial rather than leaving it implied by a marker buried in the file.
    """
    encoded = text.encode("utf-8")
    if cap <= 0 or len(encoded) <= cap:
        return text, False

    dropped = len(encoded) - cap
    marker = f"\n\n... [llmdv] {dropped} bytes dropped from the middle of this transcript ...\n\n"
    budget = max(cap - len(marker), 0)
    head = budget // 2
    tail = budget - head
    return (
        encoded[:head].decode("utf-8", errors="ignore")
        + marker
        + (encoded[-tail:].decode("utf-8", errors="ignore") if tail else ""),
        True,
    )
