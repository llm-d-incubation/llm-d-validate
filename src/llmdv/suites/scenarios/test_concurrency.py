"""Concurrent client behavior that no single-request case produces."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from ...evidence import journal
from ...http.sse import Event
from ..conftest import budget_for, check_basic_completion, check_sse_response

# Streams cancelled at once: enough that a slot or KV block held per cancel shows next.
BURST = 8


# Agents cancel in bursts, and agents run on the surfaces that carry tools.
@pytest.mark.surface_generic("tools")
@pytest.mark.disruptive("cancels a burst of concurrent generations")
@pytest.mark.case_id("scenarios.cancel_burst")
def test_cancel_burst(client, surface, model, ingress):
    """Streams the client closes mid-generation, all at once, leave the path serving.

    Agents cancel in bursts: one user interrupt stops every sub-agent stream together. Each
    cancelled request must release what it held, so health is unchanged and the request after
    the burst is served.
    """

    def has_output(event: Event) -> bool:
        body = event.json()
        if not isinstance(body, dict):
            return False
        chunk = surface.parse_chunk(body)
        return bool(chunk.content_delta or chunk.reasoning_delta) and not chunk.finish_reason

    baseline = client.get(ingress.health_path)
    reachable = (
        baseline.transport_error is None and baseline.status is not None and baseline.status < 500
    )
    journal.record("pre_burst_health", baseline.status, "reachable without 5xx", ok=reachable)
    assert reachable, f"health baseline failed: HTTP {baseline.status}"
    body = surface.stream_body(
        model.public_id,
        max_tokens=budget_for(model, floor=128),
        prompt="Count from 1 to 100, one number at a time.",
    )

    def cancelled(_: int):
        return client.stream(
            surface.path,
            json_body=body,
            stop_event=surface.stream_stop,
            stop_when=has_output,
            max_events=128,
        )

    with ThreadPoolExecutor(max_workers=BURST) as pool:
        streams = list(pool.map(cancelled, range(BURST)))
    for exchange, _ in streams:
        check_sse_response(exchange, ingress)
    closed = sum(transcript.client_closed for _, transcript in streams)
    journal.record(
        "burst_streams_closed",
        closed,
        f"all {BURST} closed by the client before their terminal event",
        ok=closed == BURST,
    )
    assert closed == BURST, f"{BURST - closed} of {BURST} streams ended before they were cancelled"
    health = client.get(ingress.health_path)
    ok = health.status == baseline.status and health.transport_error is None
    journal.record(
        "post_burst_health", health.status, f"same as baseline HTTP {baseline.status}", ok=ok
    )
    assert ok, f"health changed after the burst: HTTP {health.status}"
    check_basic_completion(
        client.post(
            surface.path,
            json_body=surface.minimal_body(model.public_id, max_tokens=budget_for(model)),
        ),
        surface,
        model,
        ingress,
    )
