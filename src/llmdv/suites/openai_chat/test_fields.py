"""Every Chat request field a client can send: accepted, ignored or refused, as declared.

The OpenAI and engine schemas list dozens of optional fields, and each ingress treats them its
own way: an engine generates with them, a gateway may drop or refuse them. The profile declares
the disposition per field in `capabilities.chat_fields`; an undeclared field skips without a
request. Whatever is declared, a 5xx fails, because no client can recover from it. Every field
the spec types as nullable is also sent as null in one request, which needs no declaration.
"""

from __future__ import annotations

import pytest

from ...evidence import journal
from ...profile.schema import CHAT_FIELDS
from ..conftest import budget_for, check_basic_completion
from ..openai_contract import field_probe, require_contract
from . import fields

pytestmark = pytest.mark.surface("chat_completions")


@pytest.mark.case_id("openai_chat.request_field")
@pytest.mark.parametrize("name", CHAT_FIELDS)
def test_chat_request_field(client, surface, model, ingress, name):
    field_probe(
        client,
        surface,
        model,
        ingress,
        name,
        ingress.capabilities.chat_fields,
        fields.probe(name),
        name in fields.STREAMED,
    )


@pytest.mark.case_id("openai_chat.null_optional_fields")
def test_chat_null_optional_fields(client, surface, model, ingress):
    """Every field the spec types as nullable, sent as null, still gets an ordinary reply.

    Clients that build bodies from typed structs can serialize unset optional fields as null.
    """
    require_contract(ingress, surface)
    nulls = dict.fromkeys(fields.NULLABLE)
    journal.record("null_fields", sorted(nulls))
    body = {**surface.minimal_body(model.public_id, budget_for(model)), **nulls}
    check_basic_completion(client.post(surface.path, json_body=body), surface, model, ingress)
