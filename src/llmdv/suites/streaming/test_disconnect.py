"""Deliberately close an unfinished generation, then check the serving path recovers."""

from __future__ import annotations

import pytest

from ...evidence import journal
from ...http.client import RecordingClient
from ...http.sse import Event
from ...profile.schema import Ingress, Model
from ...surfaces.base import Surface
from ..conftest import budget_for, check_basic_completion, check_sse_response

pytestmark = pytest.mark.disruptive("closes a generation before its terminal event")


@pytest.mark.surface("chat_completions")
@pytest.mark.case_id("openai_chat.disconnect_recovery")
def test_disconnect_recovery_chat(client, surface, model, ingress):
    _disconnect(client, surface, model, ingress)


@pytest.mark.surface("responses")
@pytest.mark.case_id("openai_responses.disconnect_recovery")
def test_disconnect_recovery_responses(client, surface, model, ingress):
    _disconnect(client, surface, model, ingress)


@pytest.mark.surface("messages")
@pytest.mark.case_id("anthropic_messages.disconnect_recovery")
def test_disconnect_recovery_messages(client, surface, model, ingress):
    _disconnect(client, surface, model, ingress)


def _disconnect(client: RecordingClient, surface: Surface, model: Model, ingress: Ingress) -> None:
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
    journal.record("pre_disconnect_health", baseline.status, "reachable without 5xx", ok=reachable)
    assert reachable, (
        f"health baseline failed: HTTP {baseline.status} {baseline.transport_error or ''}"
    )
    body = surface.stream_body(
        model.public_id,
        max_tokens=budget_for(model, floor=128),
        prompt="Count from 1 to 100, one number at a time.",
    )
    exchange, transcript = client.stream(
        surface.path,
        json_body=body,
        stop_event=surface.stream_stop,
        stop_when=has_output,
        max_events=128,
    )
    check_sse_response(exchange, ingress)
    journal.record(
        "intentional_stream_close",
        {"events": len(transcript.events), "client_closed": transcript.client_closed},
        "client closed after output before terminal",
        ok=transcript.client_closed,
    )
    assert transcript.client_closed, (
        "stream ended before an unfinished output delta could be closed"
    )
    health = client.get(ingress.health_path)
    ok = health.status == baseline.status and health.transport_error is None
    journal.record(
        "post_disconnect_health", health.status, f"same as baseline HTTP {baseline.status}", ok=ok
    )
    assert ok, (
        f"health failed after disconnect: HTTP {health.status} {health.transport_error or ''}"
    )
    check_basic_completion(
        client.post(
            surface.path,
            json_body=surface.minimal_body(model.public_id, max_tokens=budget_for(model)),
        ),
        surface,
        model,
        ingress,
    )
