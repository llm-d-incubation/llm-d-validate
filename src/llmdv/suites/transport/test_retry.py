"""What SDK retry logic meets: retry signals on errors, idempotency, retries after a timeout.

Both pinned SDKs retry twice by default on 408, 409, 429 and any 5xx, and on a connection error
or read timeout, unless the response says `x-should-retry: false`. So a layer that answers a
deterministic client error with one of those statuses triples the load and the wait for a
request that can never succeed, and a client timeout produces a second generation of the same
request. These cases check the signals a layer gives and how it treats the retried request.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

import pytest

from ...assertions.errors import assert_not_throttled
from ...assertions.transport import SDK_RETRY_STATUSES, assert_answered
from ...evidence import journal
from ...http.sse import StreamError
from ...profile.schema import Disposition
from ...profile.transport import StreamBudget
from ..conftest import budget_for, check_stream_opened, nonexistent_model_name
from .helpers import body, carrier, check_served

# Short enough that a long generation outlives it, as a client read timeout does.
RETRY_PROBE_TIMEOUT_S = 1.0


@pytest.mark.case_id("transport.sdk_retry_signal")
def test_deterministic_errors_are_not_retryable(client, model, ingress):
    """An unknown model, an invalid token limit and an unknown path are not SDK-retryable.

    None may answer 408, 409, 429 or 5xx, or carry `x-should-retry: true`.
    """
    surface = carrier(ingress)
    headers = surface.extra_headers()
    probes = {
        "unknown_model": client.post(
            surface.path,
            json_body=surface.minimal_body(nonexistent_model_name(), 8),
            headers=headers,
        ),
        "invalid_token_limit": client.post(
            surface.path,
            json_body=surface.invalid_body(model.public_id, "invalid_token_limit"),
            headers=headers,
        ),
        "unknown_path": client.post(
            f"/v1/llmdv-no-such-path-{uuid.uuid4().hex[:8]}", json_body={}, headers=headers
        ),
    }
    retryable = []
    for name, exchange in probes.items():
        assert_not_throttled(exchange, ingress)
        assert_answered(exchange, name)
        should_retry = (exchange.response_headers.get("x-should-retry") or "").lower()
        observed = {"status": exchange.status, "x-should-retry": should_retry or None}
        sdk_retries = should_retry == "true" or (
            should_retry != "false" and exchange.status in SDK_RETRY_STATUSES
        )
        journal.record(name, observed, "not retried by the pinned SDKs", ok=not sdk_retries)
        if sdk_retries:
            retryable.append(f"{name}: {observed}")
    assert not retryable, (
        "deterministic client errors that the SDKs would retry twice: " + "; ".join(retryable)
    )


@pytest.mark.case_id("transport.idempotency_key")
def test_idempotency_key(client, model, ingress):
    """Two requests with one Idempotency-Key are replayed or independent, as declared."""
    declared = ingress.transport.idempotency
    if declared is None:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no transport.idempotency")
    surface = carrier(ingress)
    key = f"llmdv-{uuid.uuid4().hex}"
    headers = {**surface.extra_headers(), "Idempotency-Key": key}
    sent = body(surface, model)
    ids = []
    for _ in range(2):
        exchange = client.post(surface.path, json_body=sent, headers=headers)
        check_served(exchange, surface, model, ingress)
        ids.append(exchange.response_body.get("id"))
    same = ids[0] is not None and ids[0] == ids[1]
    journal.record("response_ids", ids, declared, ok=same == (declared == "replayed"))
    assert same == (declared == "replayed"), (
        f"Idempotency-Key is declared {declared!r} but the two responses had ids {ids}"
    )


@pytest.mark.disruptive("abandons a long generation at a client timeout, then retries it")
@pytest.mark.case_id("transport.retry_after_timeout")
def test_retry_after_a_client_timeout(client, model, ingress):
    """A request abandoned at a client read timeout and retried, as SDKs do, is answered.

    The retry carries `x-stainless-retry-count: 1` like the SDKs' second attempt and must be
    an ordinary generation. Where `stream_budget.next_request_s` is declared, the retry's first
    output must arrive within it: a layer still busy with the abandoned generation holds the
    retry before it starts. The retry streams so that its start can be timed apart from its
    length, which a non-streamed reply hides. A declared `min_tokens_per_s` also bounds the
    whole retry by `next_request_s` plus its reported output at that rate. The retry stream is
    read for its status, output and timing only: its frame contract is the streaming suites'
    to check, and a gateway's known stream defect must not stand in for this case's verdict.
    Whether the abandoned generation kept running is not visible from here.
    """
    surface = carrier(ingress)
    prompt = "Count from 1 to 500, one number per line."
    max_tokens = budget_for(model, floor=512)
    sent = surface.minimal_body(model.public_id, max_tokens=max_tokens, prompt=prompt)
    headers = {**surface.extra_headers(), "x-stainless-retry-count": "0"}
    first = client.post(
        surface.path, json_body=sent, headers=headers, timeout=RETRY_PROBE_TIMEOUT_S
    )
    timed_out = first.transport_error is not None and "Timeout" in first.transport_error
    journal.record("first_attempt", first.transport_error or first.status, "a client timeout")
    if not timed_out:
        assert_not_throttled(first, ingress)
        pytest.skip(
            f"[requires_load] the first attempt answered {first.status} within "
            f"{RETRY_PROBE_TIMEOUT_S}s, so no timeout was exercised"
        )
    streamed = surface.stream_body(
        model.public_id, max_tokens=max_tokens, include_usage=True, prompt=prompt
    )
    retry, transcript = client.stream(
        surface.path,
        json_body=streamed,
        headers={**headers, "x-stainless-retry-count": "1"},
        stop_event=surface.stream_stop,
    )
    check_stream_opened(retry, ingress)
    chunks = [
        (event.elapsed_s, surface.parse_chunk(value))
        for event in transcript.data_events
        if isinstance(value := _json(event), dict)
    ]
    outputs = [at for at, chunk in chunks if chunk.content_delta or chunk.reasoning_delta]
    journal.record("retry_output_deltas", len(outputs), "at least one", ok=bool(outputs))
    assert outputs, "the retry streamed no output delta"
    budget = ingress.transport.stream_budget
    if budget is None or budget.next_request_s is None:
        return
    started = round((retry.headers_s or 0.0) + outputs[0], 3)
    ok = started <= budget.next_request_s
    journal.record("retry_first_output_s", started, f"at most {budget.next_request_s}s", ok=ok)
    assert ok, f"the retry's first output came after {started:.1f}s, above {budget.next_request_s}s"
    usages = [chunk.usage for _, chunk in chunks if chunk.usage is not None]
    tokens = usages[-1].completion_tokens if usages else None
    if not (budget.min_tokens_per_s and tokens):
        # Say so: a verdict that skipped its total bound must not read as one that applied it.
        reason = "no min_tokens_per_s declared" if not budget.min_tokens_per_s else "no usage"
        journal.record("retry_bound_s", f"not applied: {reason}", "start bounded only", ok=True)
        return
    bound = _retry_bound(budget, tokens)
    ok = retry.elapsed_s <= bound
    journal.record("retry_elapsed_s", round(retry.elapsed_s, 3), f"at most {bound}s", ok=ok)
    assert ok, f"the retry took {retry.elapsed_s:.1f}s, above {bound}s"


def _json(event):
    """An event's payload, or None for one that is not JSON: the frame contract is not ours."""
    try:
        return event.json()
    except StreamError:
        return None


def _retry_bound(budget: StreamBudget, completion_tokens: int) -> float:
    """The time the whole retry may take: the next-request budget plus its own generation."""
    generation_s = completion_tokens / budget.min_tokens_per_s
    bound = round(budget.next_request_s + generation_s, 3)
    journal.record(
        "retry_bound_s",
        {
            "next_request_s": budget.next_request_s,
            "completion_tokens": completion_tokens,
            "min_tokens_per_s": budget.min_tokens_per_s,
            "generation_s": round(generation_s, 3),
            "bound_s": bound,
        },
    )
    return bound


_INTEGER = re.compile(r"^\d+$")
# OpenAI's reset format: a Go-style duration such as 1s, 6m0s, 20ms or 1h2m3.5s.
_DURATION = re.compile(r"^(\d+(\.\d+)?(ms|h|m|s))+$")


def _parses(name: str, value: str) -> bool:
    if name.endswith("-reset") or "-reset-" in name:
        if name.startswith("anthropic-ratelimit-"):
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                return True
            except ValueError:
                return False
        return bool(_DURATION.match(value) or _INTEGER.match(value))
    return bool(_INTEGER.match(value))


@pytest.mark.case_id("transport.rate_limit_headers")
def test_rate_limit_header_values_parse(client, profile, model, ingress):
    """Promised rate-limit headers carry values a client can pace itself with.

    Limits and remaining counts are integers; OpenAI resets are durations and Anthropic resets
    are RFC 3339 times, per each API's rate-limit reference.
    """
    promised = sorted(
        header
        for header, per_ingress in profile.headers.items()
        if per_ingress.get(ingress.id) is Disposition.PROMISED
        and header.startswith(("x-ratelimit-", "anthropic-ratelimit-"))
    )
    if not promised:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} promises no rate-limit header")
    surface = carrier(ingress)
    exchange = client.post(
        surface.path, json_body=body(surface, model), headers=surface.extra_headers()
    )
    check_served(exchange, surface, model, ingress)
    bad = []
    for name in promised:
        value = (exchange.response_headers.get(name) or "").strip()
        ok = bool(value) and _parses(name, value)
        journal.record(name, value or None, "present and parseable", ok=ok)
        if not ok:
            bad.append(f"{name}: {value!r}")
    assert not bad, "rate-limit headers a client cannot parse: " + "; ".join(bad)
