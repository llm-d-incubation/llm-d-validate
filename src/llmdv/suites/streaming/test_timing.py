"""Stream timing a client sees: steady delivery, cancels that release the path, no buffering.

Agents put a read timeout on every stream and cancel requests during long prefills. A stream
that stalls between deltas, a cancel that leaves the next request waiting, or a compressing
layer that buffers a stream until it ends all look like a hang to the client.
"""

from __future__ import annotations

import pytest

from ...assertions.context import MIN_STREAM_SPREAD_S, STREAM_SPREAD_SHARE
from ...assertions.transport import assert_answered
from ...evidence import journal
from ...http.sse import Transcript
from ...surfaces.base import Surface
from ..conftest import budget_for, check_basic_completion, check_sse_response
from ..transport.helpers import body, carrier
from .helpers import stream_generation

LONG_ANSWER = "Count from 1 to 300, one number per line."


def _delta_times(surface: Surface, transcript: Transcript) -> list[float]:
    times = []
    for event in transcript.data_events:
        value = event.json()
        if not isinstance(value, dict):
            continue
        chunk = surface.parse_chunk(value)
        if chunk.content_delta or chunk.reasoning_delta:
            times.append(event.elapsed_s)
    return times


@pytest.mark.surface_generic()
@pytest.mark.case_id("streaming.steady_flow")
def test_stream_flows_within_budget(client, surface, model, ingress):
    """A long answer streams within the declared first-delta, gap and token-rate budget."""
    budget = ingress.transport.stream_budget
    if budget is None or (
        budget.first_event_s is None
        and budget.max_gap_s is None
        and budget.min_tokens_per_s is None
    ):
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no transport.stream_budget")
    sent = surface.stream_body(
        model.public_id,
        max_tokens=budget_for(model, floor=512),
        include_usage=True,
        prompt=LONG_ANSWER,
    )
    exchange, transcript, completion = stream_generation(client, surface, model, ingress, sent)
    times = _delta_times(surface, transcript)
    assert times, "the stream carried no output delta"
    first = (exchange.headers_s or 0.0) + times[0]
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    largest = max(gaps, default=0.0)
    span = times[-1] - times[0]
    tokens = completion.usage.completion_tokens if completion.usage else None
    rate = tokens / span if tokens and span > 0 else None
    observed = {
        "first_delta_s": round(first, 3),
        "max_gap_s": round(largest, 3),
        "tokens_per_s": round(rate, 1) if rate is not None else None,
        "deltas": len(times),
    }
    failures = []
    if budget.first_event_s is not None and first > budget.first_event_s:
        failures.append(f"first delta after {first:.2f}s, above {budget.first_event_s}s")
    if budget.max_gap_s is not None and largest > budget.max_gap_s:
        failures.append(f"a {largest:.2f}s gap between deltas, above {budget.max_gap_s}s")
    if budget.min_tokens_per_s is not None and (rate is None or rate < budget.min_tokens_per_s):
        failures.append(f"{observed['tokens_per_s']} tokens/s, below {budget.min_tokens_per_s}")
    journal.record("stream_timing", observed, "within transport.stream_budget", ok=not failures)
    assert not failures, "stream outside its declared budget: " + "; ".join(failures)


def _prefill_prompt(model) -> str:
    # Long enough that the engine is still in prefill when the client closes, and within the
    # context window with room for the answer.
    words = max(16, min(4000, (model.max_model_len or 8192) // 4))
    return " ".join(["context"] * words) + "\nSay ok."


@pytest.mark.disruptive("closes a stream during prefill, before any output")
@pytest.mark.case_id("streaming.disconnect_before_output")
def test_disconnect_before_output(client, model, ingress):
    """A stream closed as its headers arrive leaves health and the next generation unchanged."""
    surface = carrier(ingress, prefer=("chat_completions",))
    baseline = client.get(ingress.health_path)
    assert_answered(baseline, "health before the disconnect")
    sent = surface.stream_body(
        model.public_id, max_tokens=budget_for(model), prompt=_prefill_prompt(model)
    )
    exchange, transcript = client.stream(
        surface.path,
        json_body=sent,
        headers=surface.extra_headers(),
        stop_event=surface.stream_stop,
        close_on_headers=True,
    )
    check_sse_response(exchange, ingress)
    journal.record(
        "closed_on_headers",
        {"events": len(transcript.events), "client_closed": transcript.client_closed},
        "closed before any event",
        ok=transcript.client_closed and not transcript.events,
    )
    health = client.get(ingress.health_path)
    ok = health.status == baseline.status and health.transport_error is None
    journal.record("post_disconnect_health", health.status, f"HTTP {baseline.status}", ok=ok)
    assert ok, f"health changed after a disconnect: HTTP {health.status} {health.transport_error}"
    check_basic_completion(
        client.post(surface.path, json_body=body(surface, model), headers=surface.extra_headers()),
        surface,
        model,
        ingress,
    )


@pytest.mark.disruptive("closes a stream after its first output delta")
@pytest.mark.case_id("streaming.cancel_then_next")
def test_cancel_then_next(client, model, ingress):
    """The request after a cancelled stream completes within `stream_budget.next_request_s`."""
    budget = ingress.transport.stream_budget
    if budget is None or budget.next_request_s is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no "
            "transport.stream_budget.next_request_s"
        )
    surface = carrier(ingress, prefer=("chat_completions",))

    def has_output(event) -> bool:
        value = event.json()
        if not isinstance(value, dict):
            return False
        chunk = surface.parse_chunk(value)
        return bool(chunk.content_delta or chunk.reasoning_delta)

    sent = surface.stream_body(
        model.public_id, max_tokens=budget_for(model, floor=512), prompt=LONG_ANSWER
    )
    exchange, transcript = client.stream(
        surface.path,
        json_body=sent,
        headers=surface.extra_headers(),
        stop_event=surface.stream_stop,
        stop_when=has_output,
    )
    check_sse_response(exchange, ingress)
    journal.record("cancelled", transcript.client_closed, "closed after the first delta")
    assert transcript.client_closed, "the stream ended before it could be cancelled"
    following = client.post(
        surface.path, json_body=body(surface, model), headers=surface.extra_headers()
    )
    check_basic_completion(following, surface, model, ingress)
    ok = following.elapsed_s <= budget.next_request_s
    journal.record(
        "next_request_s", round(following.elapsed_s, 3), f"at most {budget.next_request_s}s", ok=ok
    )
    assert ok, (
        f"the request after a cancel took {following.elapsed_s:.2f}s, "
        f"above {budget.next_request_s}s"
    )


def _spread(times: list[float]) -> bool:
    """The rule `assert_incremental` applies: deltas spread over a share of the stream."""
    if len(times) < 2:
        return False
    return times[-1] - times[0] >= max(MIN_STREAM_SPREAD_S, STREAM_SPREAD_SHARE * times[-1])


@pytest.mark.case_id("transport.stream_encoding")
def test_stream_offered_gzip_is_not_buffered(client, model, ingress):
    """A stream offered gzip arrives as incrementally as the same stream sent uncompressed.

    A compressing layer that buffers until the stream ends turns a live stream into one late
    burst. The uncompressed stream is the baseline: if it also arrives at once, the generation
    was too fast to tell buffering apart, and the case skips.
    """
    surface = carrier(ingress, prefer=("chat_completions",))
    sent = surface.stream_body(
        model.public_id,
        max_tokens=budget_for(model, floor=512),
        include_usage=True,
        prompt=LONG_ANSWER,
    )
    spread = {}
    for coding in ("identity", "gzip"):
        exchange, transcript = client.stream(
            surface.path,
            json_body=sent,
            headers={**surface.extra_headers(), "Accept-Encoding": coding},
            stop_event=surface.stream_stop,
        )
        check_sse_response(exchange, ingress)
        surface.assert_stream(transcript)
        times = _delta_times(surface, transcript)
        spread[coding] = _spread(times)
        journal.record(
            f"{coding}_stream",
            {
                "content_encoding": exchange.response_headers.get("content-encoding"),
                "deltas": len(times),
                "spread_s": round(times[-1] - times[0], 3) if len(times) > 1 else None,
            },
            "deltas spread over the stream",
            # Only the compressed stream can fail, and only against a spread baseline.
            ok=coding == "identity" or spread["gzip"] or not spread["identity"],
        )
    if not spread["identity"]:
        pytest.skip(
            "[requires_load] the uncompressed stream also arrived at once, so buffering cannot "
            "be told apart from a fast generation"
        )
    assert spread["gzip"], "the stream offered gzip arrived at once: a layer buffered it"
