"""Every Completions request field a client can send: accepted, ignored or refused, as declared.

Declared per ingress in `capabilities.completions_fields`; an undeclared field skips without a
request, and a 5xx fails under every declaration. Every field the spec types as nullable is also
sent as null in one request, which needs no declaration.

A served field must still generate text. The probes send a sentence left open, which a model
continues; an instruction sent raw, with no chat template and no cue, may legitimately end at
once, so an empty reply to it says nothing about the field.
"""

from __future__ import annotations

import pytest

from ...evidence import journal
from ...profile.schema import COMPLETIONS_FIELDS
from ..conftest import budget_for, check_basic_completion
from ..openai_contract import field_probe, require_contract
from . import fields

pytestmark = pytest.mark.surface("completions")


@pytest.mark.case_id("openai_completions.request_field")
@pytest.mark.parametrize("name", COMPLETIONS_FIELDS)
def test_completions_request_field(client, surface, model, ingress, name):
    field_probe(
        client,
        surface,
        model,
        ingress,
        name,
        ingress.capabilities.completions_fields,
        fields.probe(name),
        name in fields.STREAMED,
        prompt=surface.reply_prompt,
    )


@pytest.mark.case_id("openai_completions.null_optional_fields")
def test_completions_null_optional_fields(client, surface, model, ingress):
    """Every field the spec types as nullable, sent as null, still gets an ordinary reply.

    Clients that build bodies from typed structs can serialize unset optional fields as null.
    """
    require_contract(ingress, surface)
    nulls = dict.fromkeys(fields.NULLABLE)
    journal.record("null_fields", sorted(nulls))
    body = {
        **surface.minimal_body(model.public_id, budget_for(model), surface.reply_prompt),
        **nulls,
    }
    check_basic_completion(client.post(surface.path, json_body=body), surface, model, ingress)
