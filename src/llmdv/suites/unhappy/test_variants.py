"""Error-path variants clients produce, and the error envelope fields clients branch on.

Each variant is a well-formed request with one ordinary mistake or edge in it. Where the API
reference requires a refusal (an empty input list, an unknown role, a missing Messages token
limit, invalid image data, an unknown thinking type), the answer is a 4xx with the surface's
envelope. Where serving is also correct (no model on a single-model fleet, a tool choice
without tools, a token limit past the window, a server tool, a sampling option), the answer is
a 2xx or a 4xx with an envelope. A 5xx never passes: SDKs retry it, so a request that can never
succeed costs three attempts and reads as an outage.

None of these is marked disruptive: each is a cheap request a healthy server refuses or serves
unremarkably, which is what a client doing something ordinary and wrong looks like.
"""

from __future__ import annotations

import pytest

from ...assertions import assert_client_error, assert_error_envelope
from ...assertions.transport import served_or_refused
from ...evidence import journal
from ...surfaces import error_variants
from ..conftest import budget_for, check_basic_completion

# The conversational surfaces, which carry roles, content blocks and tools. Completions has none
# of these, so it is not parametrized for the variants built on them.
conversational = pytest.mark.surface_generic("tools")

# Anthropic error types by status, from the Messages API errors reference.
ANTHROPIC_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    529: "overloaded_error",
}


def _generation(surface):
    if surface.name not in error_variants.GENERATION:
        pytest.skip(f"[not_implemented] surface {surface.name!r} is not a generation surface")


def _variant(surface, model, name, max_tokens=None):
    sent = error_variants.body(surface, model.public_id, name, max_tokens or budget_for(model))
    if sent is None:
        pytest.skip(f"[not_implemented] surface {surface.name!r} has no {name} request")
    journal.record("variant", name)
    return sent


def _refused(client, surface, ingress, sent, error_class):
    exchange = client.post(surface.path, json_body=sent, headers=surface.extra_headers())
    assert_client_error(exchange, ingress, error_class)
    assert_error_envelope(exchange, surface, ingress, error_class)
    return exchange


def _served_or_refused(client, surface, model, ingress, sent, error_class):
    exchange = client.post(surface.path, json_body=sent, headers=surface.extra_headers())
    if served_or_refused(exchange, surface, ingress, error_class, error_class):
        check_basic_completion(exchange, surface, model, ingress)
    return exchange


@conversational
@pytest.mark.case_id("unhappy.empty_input")
def test_empty_input(client, surface, model, ingress):
    """An empty messages or input list is refused, never generated from nothing."""
    _refused(client, surface, ingress, _variant(surface, model, "empty_input"), "empty_input")


@conversational
@pytest.mark.case_id("unhappy.invalid_role")
def test_invalid_role(client, surface, model, ingress):
    """A message with a role the API does not define is refused, never silently rendered."""
    sent = _variant(surface, model, "invalid_role")
    _refused(client, surface, ingress, sent, "invalid_role")


@pytest.mark.surface_generic()
@pytest.mark.case_id("unhappy.missing_model")
def test_missing_model(client, surface, model, ingress):
    """A request without a model is served by the deployed model or refused; never a 5xx."""
    sent = _variant(surface, model, "missing_model")
    _served_or_refused(client, surface, model, ingress, sent, "missing_model")


@conversational
@pytest.mark.case_id("unhappy.tool_choice_without_tools")
def test_tool_choice_without_tools(client, surface, model, ingress):
    """A tool choice that requires a call, with no tools, is served or refused; never a 5xx."""
    sent = _variant(surface, model, "tool_choice_without_tools")
    _served_or_refused(client, surface, model, ingress, sent, "tool_choice_without_tools")


@pytest.mark.surface_generic()
@pytest.mark.case_id("unhappy.token_limit_over_context")
def test_token_limit_over_context(client, surface, model, ingress):
    """A tiny prompt with a token limit past the window is clamped and served, or refused."""
    _generation(surface)
    if not model.max_model_len:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no max_model_len to exceed")
    limit = model.max_model_len + 1000
    sent = surface.minimal_body(model.public_id, limit, surface.reply_prompt)
    journal.record("max_tokens", limit, "above max_model_len")
    _served_or_refused(client, surface, model, ingress, sent, "token_limit_over_context")


@pytest.mark.surface("messages")
@pytest.mark.case_id("unhappy.missing_token_limit")
def test_missing_token_limit(client, surface, model, ingress):
    """A Messages request without max_tokens, which the API requires, is refused."""
    sent = _variant(surface, model, "missing_token_limit")
    _refused(client, surface, ingress, sent, "missing_token_limit")


@conversational
@pytest.mark.case_id("unhappy.invalid_image")
def test_invalid_image(client, surface, model, ingress):
    """An image whose base64 data does not decode is refused."""
    _refused(client, surface, ingress, _variant(surface, model, "invalid_image"), "invalid_image")


@conversational
@pytest.mark.case_id("unhappy.image_input")
def test_image_to_a_text_only_model(client, surface, model, ingress):
    """An ingress declared to refuse images refuses one, directly and inside a tool result.

    Serving it would put the image's base64 text into the prompt, where a screenshot costs
    thousands of tokens and can overflow the context.
    """
    if not ingress.expected_error_status("image_input"):
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no error_status image_input")
    _refused(client, surface, ingress, _variant(surface, model, "image_input"), "image_input")
    nested = error_variants.body(
        surface, model.public_id, "image_in_tool_result", budget_for(model)
    )
    if nested is not None:
        journal.record("variant", "image_in_tool_result")
        _refused(client, surface, ingress, nested, "image_input")


@pytest.mark.surface("messages")
@pytest.mark.case_id("unhappy.invalid_thinking_type")
def test_invalid_thinking_type(client, surface, model, ingress):
    """A thinking type the Messages API does not define is refused."""
    sent = _variant(surface, model, "invalid_thinking_type")
    _refused(client, surface, ingress, sent, "invalid_thinking_type")


@pytest.mark.surface("messages")
@pytest.mark.case_id("unhappy.server_tool")
def test_schemaless_server_tool(client, surface, model, ingress):
    """An Anthropic server tool, which has no input_schema, is served or refused; never a 5xx."""
    sent = _variant(surface, model, "server_tool")
    _served_or_refused(client, surface, model, ingress, sent, "server_tool")


@pytest.mark.surface("chat_completions")
@pytest.mark.case_id("unhappy.sampling_option")
def test_sampling_option(client, surface, model, ingress):
    """min_p and logit_bias are served, or refused with an envelope; never a 5xx."""
    sent = _variant(surface, model, "sampling_option")
    _served_or_refused(client, surface, model, ingress, sent, "sampling_option")


@pytest.mark.surface_generic()
@pytest.mark.case_id("unhappy.error_envelope_fields")
def test_error_envelope_fields(client, surface, model, ingress):
    """A 4xx carries the fields SDKs branch on.

    OpenAI surfaces: `error.message` and `error.type` are strings. Messages: top-level `type`
    is `error` and `error.type` is the type the Anthropic errors reference gives the status.
    """
    _generation(surface)
    sent = surface.invalid_body(model.public_id, "invalid_token_limit")
    exchange = client.post(surface.path, json_body=sent, headers=surface.extra_headers())
    assert_client_error(exchange, ingress, "invalid_token_limit")
    if not ingress.produces_envelope("invalid_token_limit"):
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares this error unstructured")
    payload = exchange.response_body if isinstance(exchange.response_body, dict) else {}
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    if surface.name == "messages":
        expected = ANTHROPIC_ERROR_TYPES.get(exchange.status)
        observed = {"type": payload.get("type"), "error.type": error.get("type")}
        ok = observed == {"type": "error", "error.type": expected}
        journal.record(
            "anthropic_error_type", observed, {"type": "error", "error.type": expected}, ok=ok
        )
        assert ok, f"Anthropic clients branch on error.type; expected {expected!r}, got {observed}"
        return
    observed = {"message": error.get("message"), "type": error.get("type")}
    ok = all(isinstance(value, str) and value for value in observed.values())
    journal.record("openai_error_fields", observed, "string message and type", ok=ok)
    assert ok, f"OpenAI clients read error.message and error.type; got {observed}"
