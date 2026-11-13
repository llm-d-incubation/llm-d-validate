"""Render pins: a fixed conversation renders to the token ids the profile declares.

A prompt pin compares input token counts, so a substitution that keeps the count passes it: one
effort word for another in a template line, or a tool schema whose keys a layer sorted. Where a
render route exists, these cases render the canonical conversation of `surfaces/variants.py`,
with one tool for the tools pin, and compare a SHA-256 digest of the token ids with the digest
declared under `capabilities.render_pins.<render surface>.<pin>`. Any change to what the engine
would receive fails. Each record carries the observed digest and length, so a run against a
known-good build supplies the declaration.
"""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...evidence import journal
from ...surfaces import variants
from ..conftest import budget_for
from ..render.helpers import render_request
from .helpers import token_digest

pytestmark = pytest.mark.surface_generic("render")


def _pin(client, surface, model, ingress, pin: str) -> None:
    declared = ingress.capabilities.render_pins.get(surface.name, {}).get(pin)
    if declared is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no render_pins.{surface.name}.{pin}"
        )
    body = variants.canonical_body(
        surface.request_surface, model.public_id, budget_for(model), pin == "tools"
    )
    if body is None:
        pytest.skip(f"[not_implemented] surface {surface.name!r} cannot carry the {pin} pin")
    token_ids = render_request(client, surface, model, ingress, body)
    observed = token_digest(token_ids)
    journal.record("canonical_render_length", len(token_ids))
    ok = observed == declared
    journal.record("canonical_render_digest", observed, f"{declared} as declared", ok=ok)
    if not ok:
        fail(
            "render_pin",
            f"digest {observed} over {len(token_ids)} token ids",
            f"{declared}; the template, tokenizer or a layer changed what the engine receives",
        )


@pytest.mark.case_id("evolution.render_pin")
def test_canonical_conversation_keeps_its_tokens(client, surface, model, ingress):
    _pin(client, surface, model, ingress, "conversation")


@pytest.mark.needs("tools")
@pytest.mark.case_id("evolution.render_pin_tools")
def test_canonical_tool_conversation_keeps_its_tokens(client, surface, model, ingress):
    _pin(client, surface, model, ingress, "tools")
