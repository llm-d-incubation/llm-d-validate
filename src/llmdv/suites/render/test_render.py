"""Repeatability and input sensitivity of native render endpoints."""

from __future__ import annotations

import pytest

from ...assertions import (
    assert_client_error,
    assert_error_envelope,
    assert_no_generation,
)
from ...evidence import journal
from ...http.client import RecordingClient
from ...profile.schema import Ingress, Model
from ...surfaces.render import Render
from ..conftest import nonexistent_model_name
from .helpers import render_request

pytestmark = pytest.mark.surface_generic("render")


def render_tokens(
    client: RecordingClient, surface: Render, model: Model, ingress: Ingress, prompt: str
) -> list[int]:
    return render_request(
        client, surface, model, ingress, surface.minimal_body(model.public_id, prompt=prompt)
    )


@pytest.mark.case_id("render.stability")
def test_render_stability(client, surface, model, ingress):
    first = render_tokens(client, surface, model, ingress, "Reply with the word alpha.")
    repeated = render_tokens(client, surface, model, ingress, "Reply with the word alpha.")
    changed = render_tokens(
        client, surface, model, ingress, "Reply with a very different word: omega."
    )
    journal.record("render_repeatable", first == repeated, True, ok=first == repeated)
    assert first == repeated, "identical requests rendered different token IDs"
    journal.record("render_input_sensitive", changed != first, True, ok=changed != first)
    assert changed != first, (
        "changed prompt rendered identical tokens; constant render stub or ignored input"
    )


@pytest.mark.case_id("render.unknown_model")
def test_render_unknown_model(client, surface, ingress):
    exchange = client.post(surface.path, json_body=surface.minimal_body(nonexistent_model_name()))
    assert_client_error(exchange, ingress, "unknown_model")
    assert_error_envelope(exchange, surface, ingress, "unknown_model")
    assert_no_generation(exchange, surface)
