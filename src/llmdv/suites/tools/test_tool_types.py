"""Tool types and definition fields beyond plain function tools.

Clients send server tools, custom tools, namespaces, input examples and fine-grained streaming
as their SDKs adopt them. A fleet may not serve them, so each case accepts a clear 4xx refusal;
what fails is a 5xx, a hang, or a served answer that breaks the API's own contract.
"""

from __future__ import annotations

import pytest

from ...surfaces.tool_wire import SERVER_TOOLS, ToolChoice, ToolDef
from ..conftest import check_completion, check_sse_response
from .common import (
    ASK_WEATHER,
    WEATHER,
    check_call_turn,
    check_refusal,
    served_or_refused,
    tool_budget,
    wire,
)

pytestmark = [pytest.mark.surface_generic("tools"), pytest.mark.needs("tools")]

REQUIRED = ToolChoice("required")


@pytest.mark.surface("messages")
@pytest.mark.parametrize("kind", sorted(SERVER_TOOLS))
@pytest.mark.case_id("tools.server_tool")
def test_server_tool(client, surface, model, ingress, kind):
    """A typed server or schema-versioned tool is served or refused with a 4xx; a 5xx fails."""
    body = wire(surface).raw_tool_request(
        model.public_id,
        "Reply with the single word: ok",
        [wire(surface).server_tool(kind)],
        tool_budget(model),
    )
    exchange = client.post(surface.path, json_body=body)
    if served_or_refused(exchange, surface, ingress):
        check_completion(surface.parse_completion(exchange.response_body), surface, model)


@pytest.mark.case_id("tools.custom_tool")
def test_custom_tool(client, surface, model, ingress):
    """A custom tool is served or refused with a 4xx; a served answer keeps the contract.

    Chat and Responses custom tools take free text; on Messages, `type: custom` is the explicit
    form of an ordinary client tool.
    """
    native = wire(surface).custom_tool("shout", "Repeat a word in capitals.")
    body = wire(surface).raw_tool_request(
        model.public_id, "Use the shout tool with the word hello.", [native], tool_budget(model)
    )
    exchange = client.post(surface.path, json_body=body)
    if served_or_refused(exchange, surface, ingress):
        check_completion(surface.parse_completion(exchange.response_body), surface, model)


@pytest.mark.surface("responses")
@pytest.mark.case_id("tools.namespace")
def test_namespace(client, surface, model, ingress):
    """A namespace of function tools is served or refused with a 4xx; a 5xx fails."""
    native = wire(surface).namespace_tool("weather", [WEATHER])
    body = wire(surface).raw_tool_request(
        model.public_id, ASK_WEATHER, [native], tool_budget(model)
    )
    exchange = client.post(surface.path, json_body=body)
    if served_or_refused(exchange, surface, ingress):
        check_completion(surface.parse_completion(exchange.response_body), surface, model)


EXAMPLES = ToolDef(
    WEATHER.name,
    WEATHER.description,
    WEATHER.parameters,
    extra={"input_examples": [{"city": "Lyon"}, {"city": "Tokyo", "unit": "c"}]},
)


@pytest.mark.surface("messages")
@pytest.mark.case_id("tools.input_examples")
def test_input_examples(client, surface, model, ingress):
    """Schema-valid input_examples are served with a valid call, or refused with a 4xx."""
    body = wire(surface).request(
        model.public_id, ASK_WEATHER, [EXAMPLES], REQUIRED, tool_budget(model)
    )
    exchange = client.post(surface.path, json_body=body)
    if served_or_refused(exchange, surface, ingress):
        completion = surface.parse_completion(exchange.response_body)
        check_call_turn(completion, surface, model, [EXAMPLES], REQUIRED, names=["get_weather"])


EAGER = ToolDef(
    WEATHER.name, WEATHER.description, WEATHER.parameters, extra={"eager_input_streaming": True}
)


@pytest.mark.surface("messages")
@pytest.mark.case_id("tools.eager_input_streaming")
def test_eager_input_streaming(client, surface, model, ingress):
    """A streamed call with eager_input_streaming is served with valid input, or refused."""
    body = wire(surface).request(
        model.public_id, ASK_WEATHER, [EAGER], REQUIRED, tool_budget(model), stream=True
    )
    exchange, transcript = client.stream(
        surface.path, json_body=body, stop_event=surface.stream_stop
    )
    if exchange.status != 200:
        check_refusal(exchange, surface, ingress)
        return
    check_sse_response(exchange, ingress)
    surface.assert_stream(transcript)
    completion = surface.assemble([surface.parse_chunk(e.json()) for e in transcript.data_events])
    check_call_turn(completion, surface, model, [EAGER], REQUIRED, names=["get_weather"])
