"""Several tool calls in one assistant turn, and all their results in one user turn."""

import uuid

import pytest

from ...assertions.messages import assert_messages_stream
from ...assertions.tools import assert_tool_calls
from ...evidence import journal
from ...surfaces.base import TOOL_NAME
from ..conftest import (
    budget_for,
    check_basic_completion,
    check_completion,
    check_json_response,
    check_sse_response,
)
from .helpers import tool_schema

pytestmark = [
    pytest.mark.surface("messages"),
    pytest.mark.needs("tools", "messages_checks:parallel_tool_calls"),
]


# Both assertions lean on this wording: every tool in one turn, then the values in the answer.
PROMPT = (
    "Call every tool once, all in this one turn. After their results, reply with only "
    "the result values."
)


def _request(surface, model):
    schemas = {f"{TOOL_NAME}_a": tool_schema(), f"{TOOL_NAME}_b": tool_schema()}
    return schemas, surface.parallel_tool_body(
        model.public_id, schemas, budget_for(model, floor=512), PROMPT
    )


def _check_round_trip(client, surface, model, ingress, body, completion, schemas):
    check_completion(completion, surface, model)
    # tool_finish_reason takes the model: it reads the profile's declared
    # `model.tools.forced_finish_reason`, because a fleet may answer a forced call with its own
    # terminal reason. Calling it is not optional -- comparing against the bound method is
    # silently always False, and recording the method crashes the results writer.
    expected = surface.tool_finish_reason(model)
    finished = completion.finish_reason == expected
    journal.record("tool_finish", completion.finish_reason, expected, ok=finished)
    assert finished, (
        f"a turn that calls tools finished with {completion.finish_reason!r}, expected {expected!r}"
    )
    calls = assert_tool_calls(completion, schemas)
    results = {call.id: uuid.uuid4().hex[:12] for call in calls}
    followup = surface.tool_results_body(body, completion, results)
    answer = check_basic_completion(
        client.post(surface.path, json_body=followup), surface, model, ingress
    )
    used = [value for value in results.values() if value in answer.text]
    ok = len(used) == len(results) and not answer.tool_calls
    journal.record(
        "tool_results_round_trip",
        {"answer": answer.text, "results_used": len(used), "calls": len(answer.tool_calls)},
        f"all {len(results)} fresh results in the answer",
        ok=ok,
    )
    assert ok, "the continuation ignored one of the tool results or called a tool again"


@pytest.mark.case_id("anthropic_messages.parallel_tool_calls")
def test_several_tool_calls_in_one_turn(client, surface, model, ingress):
    schemas, body = _request(surface, model)
    response = check_json_response(client.post(surface.path, json_body=body), ingress)
    completion = surface.parse_completion(response)
    _check_round_trip(client, surface, model, ingress, body, completion, schemas)


@pytest.mark.case_id("anthropic_messages.streamed_parallel_tool_calls")
def test_several_tool_calls_in_one_streamed_turn(client, surface, model, ingress):
    schemas, body = _request(surface, model)
    exchange, transcript = client.stream(
        surface.path, json_body={**body, "stream": True}, stop_event="message_stop"
    )
    check_sse_response(exchange, ingress)
    assert_messages_stream(transcript)
    completion = surface.assemble(
        [surface.parse_chunk(event.json()) for event in transcript.events]
    )
    _check_round_trip(client, surface, model, ingress, body, completion, schemas)
