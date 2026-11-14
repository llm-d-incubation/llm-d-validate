"""Missing/invalid version behavior is declared per ingress, not inferred from its kind."""

import pytest

from ...assertions import assert_client_error, assert_error_envelope
from ...assertions.errors import tolerates
from ..conftest import budget_for, check_basic_completion


@pytest.mark.surface("messages")
@pytest.mark.parametrize("variant", ["missing", "invalid"])
@pytest.mark.disruptive("sends a missing or invalid API version")
@pytest.mark.case_id("anthropic_messages.version_header")
def test_messages_version_header(client, surface, model, ingress, variant):
    error_class = "invalid_api_version"
    if ingress.expected_error_status(error_class) is None:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no invalid_api_version status")
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model))
    check_basic_completion(client.post(surface.path, json_body=body), surface, model, ingress)
    exchange = client.post(
        surface.path,
        json_body=body,
        headers={"anthropic-version": "llmdv-invalid-version"} if variant == "invalid" else {},
        omit_headers=("anthropic-version",) if variant == "missing" else (),
    )
    if tolerates(ingress, error_class):
        check_basic_completion(exchange, surface, model, ingress)
    else:
        assert_client_error(exchange, ingress, error_class)
        assert_error_envelope(exchange, surface, ingress, error_class)
