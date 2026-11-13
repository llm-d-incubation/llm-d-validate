"""Prompt pins: a fixed conversation renders to the input size the profile declares.

A chat template edit, a tokenizer swap under the same model id, or a gateway translation that
drops or moves content changes what the model receives while every response stays correct.
The cost shows up later as lost prefix reuse or a model that ignores part of the conversation.
These cases send one fixed conversation per surface -- a system prompt, a finished exchange and
a new question, plus one tool for the tools pin -- and require the input token count declared
under `capabilities.prompt_pins` for that surface. Declare it per ingress: a gateway and an
engine may legitimately render the same conversation differently.

A substitution that keeps the token count, such as one effort word for another, keeps this pin
green; `evolution.render_pin` compares token ids where a render route exists.
"""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...evidence import journal
from ...surfaces import variants
from ..conftest import budget_for
from .helpers import input_tokens

pytestmark = pytest.mark.surface_generic


def _pin(client, surface, model, ingress, pin: str) -> None:
    declared = ingress.capabilities.prompt_pins.get(surface.name, {}).get(pin)
    if declared is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no prompt_pins.{surface.name}.{pin}"
        )
    body = variants.canonical_body(surface, model.public_id, budget_for(model), pin == "tools")
    if body is None:
        pytest.skip(f"[not_implemented] surface {surface.name!r} cannot carry the {pin} pin")
    observed = input_tokens(client, surface, model, ingress, json_body=body)
    ok = observed == declared
    journal.record("canonical_input_tokens", observed, f"{declared} as declared", ok=ok)
    if not ok:
        fail(
            "prompt_pin",
            f"{observed} input tokens",
            f"{declared}; the template, tokenizer or a layer changed what the model receives",
        )


@pytest.mark.case_id("evolution.prompt_pin")
def test_canonical_conversation_keeps_its_size(client, surface, model, ingress):
    _pin(client, surface, model, ingress, "conversation")


@pytest.mark.surface_generic("tools")
@pytest.mark.needs("tools")
@pytest.mark.case_id("evolution.prompt_pin_tools")
def test_canonical_tool_conversation_keeps_its_size(client, surface, model, ingress):
    _pin(client, surface, model, ingress, "tools")
