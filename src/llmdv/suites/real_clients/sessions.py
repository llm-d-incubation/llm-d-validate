"""Run one bounded tool-using session and read back what the client did."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from ...assertions import fail
from ...clients.claude_code import (
    SESSION_BODY_BYTES,
    SESSION_GENERATIONS,
    SESSION_OUTPUT_TOKENS,
    invoke_session,
    session_env,
)
from ...clients.relay import ClientRelay
from ...evidence import journal
from ...http.sse import clamp
from ...log.setup import get_logger
from ...plugin import register_artifact
from .helpers import check_process, check_upstream, check_version, session_timeout

log = get_logger("sessions")

SUBAGENT_TOOLS = ("Agent", "Task")

# Text of edited files kept per file, and in total, when a session fails. The project is a
# temporary directory, so without this the code the model actually wrote -- the one artifact
# that explains a multi-file-fix failure -- is deleted as the case unwinds.
# The auto-compact window, as a share of the model's declared context. A window at or above
# the model's own limit is unreachable, so this is a ceiling rather than a preference.
COMPACT_WINDOW_SHARE = 0.5
COMPACT_WINDOW_CAP = 40_000


def compact_window(model) -> int:
    """The window to ask the client to compact at, bounded by what the fleet can reach."""
    return min(COMPACT_WINDOW_CAP, int(model.max_model_len * COMPACT_WINDOW_SHARE))


PROJECT_FILE_CAP = 64 * 1024
PROJECT_TOTAL_CAP = 512 * 1024

# A session can need a newer client than the profile's floor, and the difference is in the
# client rather than the fleet. Without this the case fails on an older pin and reads as a fleet
# defect; with it the run says which version the session needs, and nothing is sent.
SESSION_MIN_VERSION = {
    # Older clients put hook output in the user message, so the session cannot distinguish
    # a layer that drops the system entry from a client that never sent one.
    "hook": (2, 1, 154),
}


@dataclass(frozen=True)
class Session:
    project: Path
    events: list[dict[str, Any]]
    answer: str

    def tool_uses(self, nested: bool | None = None) -> list[dict[str, Any]]:
        """Tool calls the session made; `nested` selects those issued by a subagent or not."""
        found = []
        for event in self.events:
            if event.get("type") != "assistant":
                continue
            if nested is not None and bool(event.get("parent_tool_use_id")) is not nested:
                continue
            content = (event.get("message") or {}).get("content") or []
            found += [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        return found

    def most_calls_in_one_turn(self, name: str) -> int:
        """Claude Code emits one event per block, so calls are grouped by message id.

        An event without an id counts as a turn of its own: lumping those together would
        report separate turns as one.
        """
        turns: Counter[str] = Counter()
        for index, event in enumerate(self.events):
            if event.get("type") != "assistant":
                continue
            message = event.get("message") or {}
            turn = message.get("id") or f"event-{index}"
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    turns[str(turn)] += block.get("name") == name
        return max(turns.values(), default=0)

    def context_peak(self) -> int:
        """The largest prompt the session sent, cached prefix included.

        Read back from the client's own usage rather than from our relay, because what decides
        compaction is what the CLIENT thinks the conversation costs.
        """
        peak = 0
        for event in self.events:
            usage = (event.get("message") or {}).get("usage") or event.get("usage")
            if not isinstance(usage, dict):
                continue
            total = (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
            peak = max(peak, total)
        return peak

    def compactions(self) -> int:
        return sum(
            event.get("type") == "system" and event.get("subtype") == "compact_boundary"
            for event in self.events
        )


def require_session(ingress, client_binary, name: str):
    """Skip unless this session is declared and its pinned binary can run here."""
    if name not in ingress.capabilities.client_sessions:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare session {name!r}")
    _, binary = client_binary
    if (
        os.name != "posix"
        or not binary.executable.is_file()
        or not os.access(binary.executable, os.X_OK)
    ):
        pytest.skip("[requires_instrumentation] install the declared executable on a POSIX runner")
    minimum = SESSION_MIN_VERSION.get(name)
    if minimum is not None and tuple(map(int, binary.version.split("."))) < minimum:
        wanted = ".".join(str(part) for part in minimum)
        pytest.skip(
            f"[requires_instrumentation] session {name!r} needs Claude Code {wanted} or newer; "
            f"this profile pins {binary.version}"
        )
    return binary


@contextmanager
def run_session(
    client,
    model,
    binary,
    *,
    ingress,
    files: dict[str, str | bytes],
    prompt: str,
    allowed_tools: tuple[str, ...],
    env: dict[str, str] | None = None,
    restrict_tools: bool = True,
):
    """Yield the finished session while its project directory still exists."""
    with (
        TemporaryDirectory(prefix="llmdv-session-") as temporary,
        ClientRelay(
            client,
            max_generations=SESSION_GENERATIONS,
            max_output_tokens=SESSION_OUTPUT_TOKENS,
            max_body_bytes=SESSION_BODY_BYTES,
        ) as relay,
    ):
        home = Path(temporary)
        project = home / "project"
        project.mkdir()
        for relative, content in files.items():
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)
        environment = session_env(home, relay.base_url, model.public_id, env)
        check_version(binary, project, environment)
        budget = session_timeout(ingress)
        log.debug(
            "session: project=%s tools=%s restrict=%s timeout=%ss",
            project,
            allowed_tools,
            restrict_tools,
            budget,
        )
        started = time.monotonic()
        result = invoke_session(
            binary,
            project=project,
            env=environment,
            prompt=prompt,
            timeout=budget,
            allowed_tools=allowed_tools,
            restrict_tools=restrict_tools,
        )
        log.debug(
            "session finished in %.1fs: code=%s problem=%s stdout=%sB",
            time.monotonic() - started,
            result.code,
            result.problem,
            len(result.stdout.encode("utf-8")),
        )
        # The trace belongs beside the verdict, not inside a check: see register_artifact.
        _attach(
            "session-trace.jsonl",
            result.stdout,
            f"the client's event stream, {len(result.stdout.encode('utf-8'))} bytes as read",
        )
        # Upstream first, deliberately. A fleet that answered 404 or 500 fails the session, and
        # the client exits in error -- so a process check that came first would report the exit
        # and the head of its event stream, and send whoever reads the run looking at the client.
        # Blame the layer that broke. The relay bounds the session too: what it refused is
        # recorded first, so a refusal is neither lost nor read as a fleet fault.
        journal.record(
            "relay_refusals", dict(Counter(relay.rejected)), "requests the relay did not forward"
        )
        check_upstream(relay, ingress, "session_upstream_statuses")
        check_process(result, "client_process")
        events = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
        # A session that waits on a background subagent reports once per wake-up.
        results = [event for event in events if event.get("type") == "result"]
        ok = bool(results) and not any(event.get("is_error") for event in results)
        journal.record(
            "client_session",
            {
                "results": len(results),
                "turns": sum(event.get("num_turns") or 0 for event in results),
                "generations": len(relay.generations),
            },
            "at least one result and none in error",
            ok=ok,
        )
        if not ok:
            fail(
                "client_session_result", results[-1:] or result.stdout[-400:], "a successful result"
            )
        answer = "\n".join(str(event.get("result") or "") for event in results)
        try:
            yield Session(project=project, events=events, answer=answer)
        except Exception:
            # The project is about to be deleted. Whatever the session wrote is the evidence.
            # Exception rather than BaseException: a pytest skip is a BaseException, and a
            # session that skipped has nothing to explain.
            _attach("session-project.txt", _snapshot(project), "the project as the session left it")
            raise


def _attach(name: str, text: str, note: str) -> None:
    """Put a bounded copy of `text` in this case's evidence bundle, if a case is running."""
    active = journal.current()
    if active is None:
        return
    clamped, truncated = clamp(text)
    register_artifact(active.nodeid, name, clamped)
    journal.record(f"attached:{name}", {"note": note, "truncated": truncated})


def _snapshot(project: Path) -> str:
    """Every file in the project, with its text, bounded twice.

    Bounded per file so one large artefact cannot crowd out the rest, and in total so a session
    that wrote a lot cannot fill the bundle. Binary files are listed by size rather than read.
    """
    lines: list[str] = []
    budget = PROJECT_TOTAL_CAP
    for path in sorted(p for p in project.rglob("*") if p.is_file()):
        relative = path.relative_to(project)
        size = path.stat().st_size
        lines.append(f"==== {relative} ({size} bytes)")
        if budget <= 0:
            lines.append("[llmdv] omitted: snapshot budget exhausted")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            lines.append("[llmdv] not read as UTF-8 text")
            continue
        body, truncated = clamp(text, min(PROJECT_FILE_CAP, budget))
        budget -= len(body.encode("utf-8"))
        lines.append(body)
        if truncated:
            lines.append("[llmdv] truncated")
    return "\n".join(lines) + "\n"
