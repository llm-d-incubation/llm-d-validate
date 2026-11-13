"""Core unhappy paths: every exposed surface must fail correctly.

A surface that serves a good request well can still mishandle a bad one, and the ways it
mishandles them are what a client actually trips over: a 500 where a 400 belongs, a body no SDK
can parse, or -- worst -- a 200 for a request that should have been refused.

Cases assert the status and envelope shape without comparing whole error messages. Context
overflow also checks the named token limit, so a body-size rejection cannot pass in its place.

Statuses come from the ingress's declared `error_status`, because layers legitimately differ. A
layer that answers an error class without a parseable envelope declares that under
`unstructured_errors`, and the assertion inverts to require the absence -- so the deviation
stays visible and cannot quietly become permanent.

Each case runs once per surface, and the bad requests come from `surface.invalid_body`, never
from a hand-built body. A body written in one surface's shape is a garbled request on every
other surface: refused for the wrong reason, so the case passes without testing anything, or
ignored, so it fails and blames the fleet.

Seven of these cases carry `@pytest.mark.disruptive`, so they do not run where the profile
declares `fleet_policy: shared`. The line drawn is whether the request costs the fleet real work
or leaves deliberately malformed input in somebody else's logs and dashboards -- not whether the
case is important. `context_overflow` makes the engine tokenize a prompt past its limit; the six
malformed-input cases are cheap but noisy, and a shared fleet's logs are not ours to fill.

Deliberately NOT marked: `invalid_token_limit`, `unsupported_option`, `unknown_path`,
`wrong_method` and `bad_credentials`. Each sends a well-formed request that a healthy server
refuses cheaply and unremarkably, which is exactly what a client doing something ordinary and
wrong looks like. Excluding those would cost real coverage for no benefit.

Raw httpx throughout. The SDKs normalise away malformed JSON, wrong field types and wrong
methods, which is precisely why they are not the instrument here.
"""

from __future__ import annotations

import json
import uuid

import pytest

from ...assertions import (
    assert_client_error,
    assert_context_limit_error,
    assert_error_envelope,
    assert_not_throttled,
    tolerates,
)
from ...evidence import journal
from ...profile.schema import AuthType

pytestmark = pytest.mark.surface_generic

# Bound the body intermediaries must receive while retaining coverage of 128K-token fleets.
# With the fixed word margin below, those probes fit comfortably within 1 MiB.
CONTEXT_OVERFLOW_MAX_BODY_BYTES = 1024 * 1024


def _check(exchange, surface, ingress, error_class):
    """Status per the ingress's declaration, then the envelope shape it is declared to produce."""
    assert_client_error(exchange, ingress, error_class)
    assert_error_envelope(exchange, surface, ingress, error_class)


def _body(model, surface, **extra):
    return surface.minimal_body(model.public_id, max_tokens=8, **extra)


def _invalid(model, surface, error_class, stream=False):
    return surface.invalid_body(model.public_id, error_class, stream=stream)


# ------------------------------------------------------------------ malformed input


@pytest.mark.disruptive("sends a body the server cannot parse")
@pytest.mark.case_id("unhappy.malformed_json")
def test_malformed_json(client, surface, model, ingress):
    """A truncated body is rejected, not parsed as far as it goes."""
    # Any proper prefix of a JSON object is invalid JSON, whatever the surface's shape.
    whole = json.dumps(_body(model, surface))
    exchange = client.post(surface.path, raw_body=whole[: len(whole) // 2])
    _check(exchange, surface, ingress, "malformed_json")


@pytest.mark.disruptive("sends a request with a required field removed")
@pytest.mark.case_id("unhappy.missing_required_field")
def test_missing_required_field(client, surface, model, ingress):
    exchange = client.post(
        surface.path, json_body=_invalid(model, surface, "missing_required_field")
    )
    _check(exchange, surface, ingress, "missing_required_field")


@pytest.mark.disruptive("sends a field with the wrong type")
@pytest.mark.case_id("unhappy.wrong_field_type")
def test_wrong_field_type(client, surface, model, ingress):
    """A required field sent with the wrong JSON type."""
    exchange = client.post(surface.path, json_body=_invalid(model, surface, "wrong_field_type"))
    _check(exchange, surface, ingress, "wrong_field_type")


# ------------------------------------------------------------------ bad values


@pytest.mark.case_id("unhappy.invalid_token_limit")
def test_invalid_token_limit(client, surface, model, ingress):
    exchange = client.post(surface.path, json_body=_invalid(model, surface, "invalid_token_limit"))
    _check(exchange, surface, ingress, "invalid_token_limit")


@pytest.mark.disruptive("makes the engine tokenize a prompt past its context limit")
@pytest.mark.case_id("unhappy.context_overflow")
def test_context_overflow(client, surface, model, ingress):
    """A prompt past the model's context length is refused, not silently truncated.

    Silent truncation is the failure worth catching: the request succeeds, the answer is drawn
    from a prompt the client did not send, and nothing in the response says so.
    """
    limit = model.max_model_len
    journal.record("declared_max_model_len", limit)
    if not limit:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no max_model_len to exceed")

    # A fixed margin avoids scaling excess input with the fleet's context window. Confirm
    # the actual refusal names the context limit rather than assuming any 4xx proves it.
    words = limit + 64
    cap = CONTEXT_OVERFLOW_MAX_BODY_BYTES
    journal.record("context_overflow_body_cap_bytes", cap)
    # Size grows linearly with repeated words. Match httpx's compact UTF-8 JSON encoding
    # and measure through the surface before allocating the prompt.
    empty, one_word = (
        len(
            json.dumps(
                _body(model, surface, prompt=prompt),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        for prompt in ("", "word ")
    )
    projected = empty + (one_word - empty) * words
    journal.record("context_overflow_projected_body_bytes", projected)
    if projected > cap:
        pytest.skip(
            f"[not_implemented] context-overflow probe projects to {projected} "
            f"bytes, above the {cap}-byte body cap for max_model_len={limit}; "
            "this bounded probe cannot exercise that context limit"
        )

    body = _body(model, surface, prompt="word " * words)
    exchange = client.post(surface.path, json_body=body)

    generated = surface.looks_like_success(exchange.response_body)
    journal.record("context_overflow_generation", generated, "no generation", ok=not generated)
    assert not generated, (
        "the context-overflow probe produced a completion instead of a context-limit "
        "rejection; check for silent truncation or a tokenizer/profile mismatch"
    )
    _check(exchange, surface, ingress, "context_overflow")
    assert_context_limit_error(exchange, surface, ingress, limit)


@pytest.mark.case_id("unhappy.unsupported_option")
def test_unsupported_option(client, surface, model, ingress):
    """An option the API does not define is rejected rather than ignored.

    Accepting it silently is the dangerous outcome: a client believes a setting took effect when
    nothing read it, which is indistinguishable from the setting not working.
    """
    option = f"llmdv_unsupported_{uuid.uuid4().hex[:8]}"
    exchange = client.post(surface.path, json_body=_body(model, surface, **{option: True}))
    journal.record("sent_option", option)

    if tolerates(ingress, "unsupported_option"):
        # Declared as ignored rather than refused. Assert exactly that, so the client-visible
        # consequence is on the record and a layer that starts enforcing it fails here.
        assert_client_error(exchange, ingress, "unsupported_option")
        journal.record(
            "unknown_option_ignored", True, f"{ingress.id} accepts unknown fields silently", ok=True
        )
        return

    _check(exchange, surface, ingress, "unsupported_option")


# ------------------------------------------------------------------ tools


@pytest.mark.surface_generic("tools")
@pytest.mark.disruptive("sends a malformed tool definition")
@pytest.mark.case_id("unhappy.invalid_tool_schema")
def test_invalid_tool_schema(client, surface, model, ingress):
    """A tool definition that does not satisfy the surface's tool schema."""
    exchange = client.post(surface.path, json_body=_invalid(model, surface, "invalid_tool_schema"))
    _check(exchange, surface, ingress, "invalid_tool_schema")


@pytest.mark.surface_generic("tools")
@pytest.mark.disruptive("sends a tool_choice naming no declared tool")
@pytest.mark.case_id("unhappy.invalid_tool_choice")
def test_invalid_tool_choice(client, surface, model, ingress):
    """A tool choice that names no declared tool."""
    exchange = client.post(surface.path, json_body=_invalid(model, surface, "invalid_tool_choice"))
    _check(exchange, surface, ingress, "invalid_tool_choice")


# ------------------------------------------------------------------ transport and routing


@pytest.mark.case_id("unhappy.unknown_path")
def test_unknown_path(client, ingress):
    """Independent of any surface, so it runs once per ingress rather than once per surface."""
    exchange = client.post(f"/v1/llmdv-no-such-path-{uuid.uuid4().hex[:8]}", json_body={})
    assert_client_error(exchange, ingress, "unknown_path")


@pytest.mark.case_id("unhappy.wrong_method")
def test_wrong_method(client, surface, model, ingress):
    """GET on a POST-only path."""
    exchange = client.get(surface.path)
    assert_client_error(exchange, ingress, "wrong_method")


@pytest.mark.case_id("unhappy.bad_credentials")
def test_bad_credentials(client, surface, model, ingress):
    """An invalid credential is refused.

    Gated on the ingress actually authenticating. On an unauthenticated layer a bogus token is
    correctly ignored, and asserting a 401 there would fail a healthy fleet.
    """
    if ingress.auth.type is AuthType.NONE:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not authenticate")

    bogus = f"llmdv-invalid-{uuid.uuid4().hex[:12]}"
    headers = ingress.auth.header(bogus)
    exchange = client.post(surface.path, json_body=_body(model, surface), headers=headers)
    journal.record("auth_scheme", ingress.auth.type.value, "rejects an invalid credential")
    assert_client_error(exchange, ingress, "bad_credentials")
    assert_error_envelope(exchange, surface, ingress, "bad_credentials")


# ------------------------------------------------------------------ streaming


@pytest.mark.disruptive("sends an invalid request that asks for a stream")
@pytest.mark.case_id("unhappy.streaming_validation")
def test_streaming_validation_failure(client, surface, model, ingress):
    """A request that asks for streaming and is invalid fails before the stream opens.

    The failure must arrive as an ordinary error response. A layer that opens a 200 stream and
    then reports the problem inside a frame -- or closes without one -- leaves a client parsing
    an error out of a transport it was told succeeded.
    """
    body = _invalid(model, surface, "wrong_field_type", stream=True)
    exchange, transcript = client.stream(surface.path, json_body=body)
    # Throttle check only: this case expects a 4xx, so the 200 assertion in
    # check_stream_opened would invert what it is testing.
    assert_not_throttled(exchange, ingress)

    journal.record("frames_received", len(transcript.events), "none", ok=not transcript.events)
    assert not transcript.events, (
        f"the stream opened and delivered {len(transcript.events)} frame(s) for a request that "
        f"failed validation; the error should arrive as a response, not inside a stream"
    )
    _check(exchange, surface, ingress, "streaming_validation")
