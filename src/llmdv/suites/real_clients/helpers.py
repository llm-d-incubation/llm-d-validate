"""Native request evidence and client process results must both support the verdict."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from ...assertions import fail
from ...assertions.errors import assert_not_throttled
from ...clients.process import ProcessResult, run_process
from ...evidence import journal
from ...http.client import Exchange
from ...http.sse import Transcript, clamp
from ...profile.schema import ClientBinary, Ingress, Model
from ...surfaces.base import Completion, Surface
from ..conftest import check_basic_completion, check_completion, check_sse_response

# The longest a multi-turn client session may run, however long the profile lets one request take.
SESSION_TIMEOUT = 300


def session_timeout(ingress: Ingress) -> float:
    """A session's bound: the profile's request timeout, capped. A profile that declares a
    short request timeout meant it."""
    return min(ingress.timeouts.request, SESSION_TIMEOUT)


def check_process(result: ProcessResult, name: str) -> None:
    """Record that a bounded child process succeeded, without carrying its output into a check.

    A check's payload is written to results.jsonl, which `llmdv compare` reads back for every run
    in its window. A session reads up to SESSION_STDOUT_BYTES of event trace, so putting the
    failing output here unclamped made one bad case worth megabytes on every later comparison.
    The ends are what a reader needs, so the check carries a clamped view and says so; the full
    trace goes to the evidence bundle as a file, where it is written once.

    The client's own result event leads the payload when it wrote one: a session's stdout opens
    with a start-up event several kilobytes long, and the reason a reader sees is the head of
    this payload, so without it the verdict shows the client's tool list rather than its error.
    """
    if result.code != 0 or result.problem is not None:
        stdout, stdout_cut = clamp(result.stdout)
        stderr, stderr_cut = clamp(result.stderr)
        fail(
            name,
            {
                "code": result.code,
                "problem": result.problem,
                "result": client_result_event(result.stdout),
                "stdout": stdout,
                "stderr": stderr,
                "stdout_bytes": len(result.stdout.encode("utf-8")),
                "stderr_bytes": len(result.stderr.encode("utf-8")),
                "truncated": stdout_cut or stderr_cut,
            },
            "successful bounded process",
        )
    journal.record(
        name,
        {
            "code": result.code,
            "stdout_chars": len(result.stdout),
            "stderr_chars": len(result.stderr),
        },
        "successful bounded process",
        ok=True,
    )


RESULT_FIELDS = ("subtype", "is_error", "api_error_status", "terminal_reason", "result")
RESULT_TEXT_CAP = 4096


def client_result_event(stdout: str) -> dict[str, Any] | None:
    """The fields of the client's last `result` event that say how it ended, if it wrote one."""
    found = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            found = event
    if found is None:
        return None
    fields = {field: found[field] for field in RESULT_FIELDS if field in found}
    # The result text can carry the fleet's whole error body; keep it to a reason's length.
    if isinstance(fields.get("result"), str):
        fields["result"] = clamp(fields["result"], RESULT_TEXT_CAP)[0]
    return fields


def check_upstream(relay, ingress: Ingress, name: str, since: int = 0) -> None:
    """Every generation the relay forwarded since `since` must have been served 200.

    Checked before the client's own process and result, deliberately. A client that is answered
    404 or 500 exits in error, and may repeat the generation first -- Claude Code falls back
    from streaming to non-streaming, which the relay refuses as a retry. A verdict that reports
    the exit or the refusal sends whoever reads the run looking at the client or at llmdv. The
    status is the finding, so it decides the verdict, and any refusal is carried beside it.

    The client's own result is not enough evidence either. A session is allowed many
    generations, and the Anthropic SDK retries 5xx -- so an ingress can answer 500, the client
    can retry, and the session can still finish with a successful result while the fleet was
    failing. This is not hypothetical on a gateway that turns an upstream 4xx into a 5xx: a 4xx
    is final and a 5xx invites the retry that hides it. The relay turns a transport failure into
    502 for the same reason, which the SDK also retries.

    A declared 429 is throttling rather than a fault, so it goes through `assert_not_throttled`
    and skips the case; an undeclared one stays a failure. A refusal alone is not asserted here:
    the relay's own bounds legitimately refuse a request, and each case records and judges that
    itself.
    """
    generations = relay.generations[since:]
    for exchange, _ in generations:
        assert_not_throttled(exchange, ingress)
    statuses = Counter(
        "transport_error" if exchange.transport_error else exchange.status
        for exchange, _ in generations
    )
    if any(status != 200 for status in statuses):
        observed: dict[str, Any] = {"statuses": dict(statuses)}
        if relay.rejected:
            observed["relay_refused"] = list(relay.rejected)
        fail(name, observed, "every generation served 200")
    journal.record(name, dict(statuses), "every generation served 200", ok=True)


def check_version(binary: ClientBinary, directory: Path, env: dict[str, str]) -> None:
    result = run_process([str(binary.executable), "--version"], cwd=directory, env=env, timeout=10)
    check_process(result, "client_version_process")
    found = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)(?: \(Claude Code\))?", result.stdout.strip())
    actual = found.group(1) if found else None
    journal.record("client_version", actual, binary.version, ok=actual == binary.version)
    assert actual == binary.version, (
        f"client version {actual!r} differs from pin {binary.version!r}"
    )


def check_client_result(result: ProcessResult, model: Model) -> dict[str, Any]:
    check_process(result, "client_process")
    try:
        body = json.loads(result.stdout)
    except ValueError:
        body = None
    ok = (
        isinstance(body, dict)
        and body.get("type") == "result"
        and body.get("subtype") == "success"
        and body.get("is_error") is False
        and isinstance(body.get("result"), str)
        and bool(body["result"].strip())
    )
    if not ok:
        fail("client_result", body, "successful result with answer text")
    journal.record(
        "client_result", {"answer_chars": len(body["result"])}, "successful answer", ok=True
    )
    used = body.get("modelUsage")
    ok = isinstance(used, dict) and bool(used) and set(used) <= set(model.all_names)
    journal.record(
        "client_models", list(used) if isinstance(used, dict) else used, model.all_names, ok=ok
    )
    assert ok, "client used an undeclared model or omitted model usage"
    return body


def check_native_generation(
    generation: tuple[Exchange, Transcript | None], surface: Surface, model: Model, ingress: Ingress
) -> Completion:
    exchange, transcript = generation
    if transcript is None:
        return check_basic_completion(exchange, surface, model, ingress)
    check_sse_response(exchange, ingress)
    surface.assert_stream(transcript)
    completion = surface.assemble(
        [surface.parse_chunk(event.json()) for event in transcript.data_events]
    )
    check_completion(completion, surface, model)
    return completion
