"""Authenticated success and unauthenticated rejection must both be observable."""

import pytest

from ...assertions import assert_client_error, assert_error_envelope
from ...profile.schema import AuthType
from ..conftest import budget_for, check_basic_completion


@pytest.mark.surface_generic()
@pytest.mark.case_id("headers.required_authentication")
def test_required_authentication(client, surface, model, ingress):
    if ingress.auth.type is AuthType.NONE:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not authenticate")
    body = surface.minimal_body(model.public_id, budget_for(model), surface.reply_prompt)
    check_basic_completion(client.post(surface.path, json_body=body), surface, model, ingress)
    exchange = client.post(
        surface.path, json_body=body, omit_headers=("Authorization", "x-api-key")
    )
    assert_client_error(exchange, ingress, "bad_credentials")
    assert_error_envelope(exchange, surface, ingress, "bad_credentials")
