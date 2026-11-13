"""Native render request validation shared by smoke and prefix fixtures."""

from __future__ import annotations

from typing import Any

from ...assertions import assert_not_throttled
from ...assertions.render import assert_rendered
from ...evidence import journal
from ...http.client import RecordingClient
from ...profile.schema import Ingress, Model
from ...surfaces.render import Render


def render_request(
    client: RecordingClient, surface: Render, model: Model, ingress: Ingress, body: dict[str, Any]
) -> list[int]:
    exchange = client.post(surface.path, json_body=body)
    assert_not_throttled(exchange, ingress)
    ok = exchange.status == 200 and exchange.transport_error is None
    journal.record("render_http_status", exchange.status, "HTTP 200", ok=ok)
    assert ok, f"render failed: HTTP {exchange.status}, {exchange.transport_error}"
    rendered = surface.parse_render(exchange.response_body)
    assert_rendered(rendered, model)
    return rendered.token_ids
