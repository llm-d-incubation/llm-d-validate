"""Shared stream checks, with each surface owning its grammar."""

from __future__ import annotations

from typing import Any

import pytest

from ...http.client import Exchange, RecordingClient
from ...http.sse import Transcript
from ...profile.schema import Ingress, Model
from ...surfaces.base import Completion, Surface
from ..conftest import check_completion, check_sse_response


def check_stream_completion(
    client: RecordingClient, surface: Surface, model: Model, ingress: Ingress, body: dict[str, Any]
) -> Completion:
    return stream_generation(client, surface, model, ingress, body)[2]


def stream_generation(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    body: dict[str, Any],
    *,
    idle_timeout: float | None = None,
) -> tuple[Exchange, Transcript, Completion]:
    """Stream one generation and check its grammar and the shared generation contract.

    `idle_timeout` replaces the ingress's `stream_idle` for this stream.
    """
    if not surface.has_stream_lifecycle:
        pytest.skip(f"[not_implemented] surface {surface.name!r} has no stream lifecycle assertion")
    exchange, transcript = client.stream(
        surface.path, json_body=body, stop_event=surface.stream_stop, idle_timeout=idle_timeout
    )
    check_sse_response(exchange, ingress)
    surface.assert_stream(transcript)
    completion = surface.assemble([surface.parse_chunk(e.json()) for e in transcript.data_events])
    check_completion(completion, surface, model)
    return exchange, transcript, completion
