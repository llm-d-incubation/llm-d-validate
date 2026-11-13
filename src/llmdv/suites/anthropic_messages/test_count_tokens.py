"""Count Messages input without generating output or assuming a particular tokenizer."""

from __future__ import annotations

import pytest
from anthropic.types import MessageTokensCount

from ...assertions import assert_client_error, assert_error_envelope
from ...assertions.messages import assert_messages_token_count
from ...evidence import journal
from ..conftest import check_json_response, nonexistent_model_name, throttle_aware

pytestmark = [pytest.mark.surface("messages"), pytest.mark.needs("count_tokens")]


@pytest.fixture
def count_body(surface, model):
    return surface.count_tokens_body(model.public_id, system="You are a helpful assistant.")


@pytest.mark.case_id("anthropic_messages.count_tokens")
def test_messages_count_tokens(client, surface, ingress, count_body):
    exchange = client.post(surface.count_tokens_path, json_body=count_body)
    baseline = assert_messages_token_count(check_json_response(exchange, ingress))

    expanded = {
        **count_body,
        "messages": [
            *count_body["messages"],
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "Please also count this additional message."},
        ],
    }
    exchange = client.post(surface.count_tokens_path, json_body=expanded)
    count = assert_messages_token_count(check_json_response(exchange, ingress))
    journal.record(
        "input_tokens_grow",
        {"baseline": baseline, "expanded": count},
        "expanded > baseline",
        ok=count > baseline,
    )
    assert count > baseline, (
        f"adding two messages did not increase input_tokens ({baseline} -> {count})"
    )


@pytest.mark.case_id("anthropic_messages.count_tokens_parses_with_anthropic_sdk")
def test_messages_count_tokens_parses_with_pinned_anthropic_sdk(
    anthropic_client, ingress, count_body
):
    with throttle_aware(ingress):
        result = anthropic_client.messages.count_tokens(**count_body)
    journal.record(
        "sdk_response_type",
        type(result).__name__,
        "MessageTokensCount",
        ok=isinstance(result, MessageTokensCount),
    )
    assert isinstance(result, MessageTokensCount), (
        "pinned Anthropic SDK did not parse a token count"
    )
    assert_messages_token_count(result.to_dict())


@pytest.mark.case_id("anthropic_messages.count_tokens_unknown_model")
def test_messages_count_tokens_rejects_unknown_model(client, surface, ingress, count_body):
    body = {**count_body, "model": nonexistent_model_name()}
    exchange = client.post(surface.count_tokens_path, json_body=body)
    assert_client_error(exchange, ingress, "unknown_model")
    assert_error_envelope(exchange, surface, ingress, "unknown_model")


@pytest.mark.disruptive("sends missing or malformed messages to the token-count endpoint")
@pytest.mark.case_id("anthropic_messages.count_tokens_invalid_messages")
@pytest.mark.parametrize("error_class", ["missing_required_field", "wrong_field_type"])
def test_messages_count_tokens_rejects_invalid_messages(
    client, surface, model, ingress, error_class
):
    body = surface.invalid_count_tokens_body(model.public_id, error_class)
    exchange = client.post(surface.count_tokens_path, json_body=body)
    assert_client_error(exchange, ingress, error_class)
    assert_error_envelope(exchange, surface, ingress, error_class)
