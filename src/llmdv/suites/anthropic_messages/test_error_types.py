"""Messages errors carry the error.type their status calls for, because clients branch on it.

The shared unhappy-path cases check the status and that an envelope parses. Anthropic SDKs and
agents go further and switch on `error.type`: `invalid_request_error` is not retried,
`overloaded_error` and `api_error` are. A status-text type such as `BadRequestError` matches none
of them.
"""

import pytest

from ...assertions import assert_client_error, assert_error_envelope, assert_not_throttled
from ...assertions.anthropic import assert_error_type
from ...evidence import journal
from ..conftest import nonexistent_model_name

pytestmark = [pytest.mark.surface("messages"), pytest.mark.needs("messages_checks:error_types")]

# A request without a required field is malformed input, which stays out of a shared fleet's logs.
TRIGGERS = [
    pytest.param(
        "missing_max_tokens",
        marks=pytest.mark.disruptive("sends a Messages request without max_tokens"),
    ),
    "invalid_thinking_type",
    "thinking_budget_too_small",
    "unknown_model",
    "token_count_unknown_model",
]


def _request(surface, model, ingress, trigger):
    if trigger == "token_count_unknown_model":
        if not ingress.capabilities.count_tokens:
            pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare count_tokens")
        body = surface.count_tokens_body(nonexistent_model_name())
        return surface.count_tokens_path, body, "unknown_model"
    if trigger == "unknown_model":
        return surface.path, surface.minimal_body(nonexistent_model_name()), "unknown_model"
    if trigger == "missing_max_tokens":
        body = surface.minimal_body(model.public_id)
        del body["max_tokens"]
        return surface.path, body, "missing_required_field"
    if trigger == "invalid_thinking_type":
        body = surface.thinking_body(model.public_id)
        body["thinking"] = {"type": "llmdv-not-a-mode"}
        return surface.path, body, None
    # The API's minimum manual budget is 1024 tokens.
    return surface.path, surface.thinking_body(model.public_id, "enabled", budget_tokens=512), None


@pytest.mark.case_id("anthropic_messages.error_type")
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_error_type_matches_status(client, surface, model, ingress, trigger):
    path, body, error_class = _request(surface, model, ingress, trigger)
    journal.record("error_trigger", trigger)
    exchange = client.post(path, json_body=body)
    if error_class is None:
        assert_not_throttled(exchange, ingress)
        status = exchange.status
        ok = exchange.transport_error is None and status is not None and 400 <= status < 500
        journal.record(f"{trigger}_status", status, "a 4xx", ok=ok)
        assert ok, f"expected a 4xx for {trigger}, got {status}"
    else:
        assert_client_error(exchange, ingress, error_class)
    assert_error_envelope(exchange, surface, ingress, error_class)
    if error_class is None or ingress.produces_envelope(error_class):
        assert_error_type(exchange, surface)
