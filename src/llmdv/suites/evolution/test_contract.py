"""Contract drift sentinels: response, stream and error shapes against a declared baseline.

An engine or gateway upgrade that renames a field, moves usage to another frame or changes a
value's type breaks every client reading the old shape, while each response still parses and
passes the ordinary checks. Each case sends one request on one surface (an ordinary one, an
unknown model, a negative token limit, or a forced tool call) and compares the key paths and
JSON types it got with the baseline the ingress declares under
`capabilities.contract_baseline.<surface>.<part>`. A missing or retyped path fails and is named
in the failure; a new path is recorded and passes. A nightly comparison then shows the case
going from pass to fail, and the failure names the field.

Undeclared parts skip as `undeclared` without a request.
"""

from __future__ import annotations

import pytest

from ...assertions import assert_healthy_finish, assert_not_throttled, fail
from ...assertions.contract import assert_contract, shape, stream_shape
from ...evidence import journal
from ..conftest import (
    budget_for,
    check_basic_completion,
    check_json_response,
    check_sse_response,
    nonexistent_model_name,
)
from ..tools.helpers import check_tool_completion, tool_schema

pytestmark = pytest.mark.surface_generic


def _baseline(ingress, surface, part: str):
    declared = ingress.capabilities.contract_baseline.get(surface.name, {}).get(part)
    if declared is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no contract_baseline."
            f"{surface.name}.{part}"
        )
    return declared


@pytest.mark.case_id("evolution.response_contract")
def test_response_keeps_its_contract(client, surface, model, ingress):
    declared = _baseline(ingress, surface, "response")
    body = surface.minimal_body(model.public_id, budget_for(model), surface.reply_prompt)
    exchange = client.post(surface.path, json_body=body)
    check_basic_completion(exchange, surface, model, ingress)
    assert_contract(shape(exchange.response_body), declared, "response")


@pytest.mark.case_id("evolution.stream_contract")
def test_stream_keeps_its_contract(client, surface, model, ingress):
    declared = _baseline(ingress, surface, "stream")
    body = surface.stream_body(model.public_id, budget_for(model), include_usage=True)
    _stream_shape(client, surface, ingress, body, declared, "stream")


def _stream_shape(client, surface, ingress, body, declared, part: str) -> None:
    exchange, transcript = client.stream(
        surface.path, json_body=body, stop_event=surface.stream_stop
    )
    check_sse_response(exchange, ingress)
    frames = [event.json() for event in transcript.data_events]
    assert_contract(stream_shape(frames), declared, part)
    chunks = [surface.parse_chunk(frame) for frame in frames]
    surface.assert_stream(transcript)
    completion = surface.assemble(chunks)
    assert_healthy_finish(completion)


def _error_shape(exchange, ingress, declared, part: str, cause: str) -> None:
    assert_not_throttled(exchange, ingress)
    status = exchange.transport_error or exchange.status
    rejected = exchange.status is not None and 400 <= exchange.status < 500
    journal.record("error_status", status, "a 4xx", ok=rejected)
    if not rejected:
        fail("error_status", status, f"a 4xx for {cause}")
    if not isinstance(exchange.response_body, dict):
        fail("error_body", type(exchange.response_body).__name__, "a JSON object")
    assert_contract(shape(exchange.response_body), declared, part)


@pytest.mark.case_id("evolution.error_contract")
def test_error_keeps_its_contract(client, surface, model, ingress):
    """The error a client meets most often by mistake: a model name that does not exist."""
    declared = _baseline(ingress, surface, "error")
    body = surface.minimal_body(nonexistent_model_name(), budget_for(model))
    exchange = client.post(surface.path, json_body=body)
    _error_shape(exchange, ingress, declared, "error", "an unknown model")


@pytest.mark.case_id("evolution.invalid_contract")
def test_validation_error_keeps_its_contract(client, surface, model, ingress):
    """A request the engine refuses in validation: a negative token limit.

    A router or gateway often answers the unknown-model error itself, so this part pins the
    envelope the engine produces, which an engine release can change on its own.
    """
    declared = _baseline(ingress, surface, "invalid")
    body = surface.invalid_body(model.public_id, "invalid_token_limit")
    exchange = client.post(surface.path, json_body=body)
    _error_shape(exchange, ingress, declared, "invalid", "a negative token limit")


@pytest.mark.surface_generic("tools")
@pytest.mark.needs("tools")
@pytest.mark.case_id("evolution.tool_response_contract")
def test_tool_call_keeps_its_contract(client, surface, model, ingress):
    """A forced tool call: parser and gateway releases change this shape on their own.

    The shape is compared before the call is parsed, so a drifted field is named even where it
    also breaks parsing.
    """
    declared = _baseline(ingress, surface, "tool_response")
    schema = tool_schema()
    body = surface.tool_body(model.public_id, schema, budget_for(model, floor=256))
    raw = check_json_response(client.post(surface.path, json_body=body), ingress)
    assert_contract(shape(raw), declared, "tool_response")
    check_tool_completion(surface.parse_completion(raw), surface, model, schema)


@pytest.mark.surface_generic("tools")
@pytest.mark.needs("tools")
@pytest.mark.case_id("evolution.tool_stream_contract")
def test_tool_call_stream_keeps_its_contract(client, surface, model, ingress):
    """The same forced tool call streamed, with usage requested where the API asks for it."""
    declared = _baseline(ingress, surface, "tool_stream")
    budget = budget_for(model, floor=256)
    stream = surface.stream_body(model.public_id, budget, include_usage=True)
    body = {
        **surface.tool_body(model.public_id, tool_schema(), budget),
        **{key: stream[key] for key in ("stream", "stream_options") if key in stream},
    }
    _stream_shape(client, surface, ingress, body, declared, "tool_stream")
