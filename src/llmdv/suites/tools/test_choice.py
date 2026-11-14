"""Every tool_choice mode on each API's own grammar.

Cases that leave the choice to the model also need `model.tools.auto_choice`, because an engine
serves automatic choice only when it is enabled. Forcing choices need only a declared parser.
"""

from __future__ import annotations

import json

import pytest

from ...assertions.tool_calls import assert_answer_contains
from ...evidence import journal
from ...surfaces.tool_wire import ToolChoice, ToolOutput
from ..conftest import check_basic_completion
from .common import (
    ASK_WEATHER,
    TIME,
    WEATHER,
    check_call_turn,
    check_text_turn,
    fresh,
    post_tools,
    require_auto,
    served_or_refused,
    tool_budget,
    wire,
)

pytestmark = [pytest.mark.surface_generic("tools"), pytest.mark.needs("tools")]


@pytest.mark.case_id("tools.auto_call")
def test_auto_call_round_trip(client, surface, model, ingress):
    """Auto calls the tool the prompt needs, and the result it gets back reaches the answer."""
    require_auto(model, ingress)
    prompt = (
        "What is the weather in Paris right now? Use the get_weather tool, then answer with "
        "the exact sky value it returns."
    )
    tools, choice = [WEATHER, TIME], ToolChoice("auto")
    body = wire(surface).request(model.public_id, prompt, tools, choice, tool_budget(model))
    completion = post_tools(client, surface, ingress, body)
    [(call, arguments)] = check_call_turn(
        completion, surface, model, tools, choice, names=["get_weather"]
    )
    city = str(arguments.get("city", ""))
    journal.record("call_city", city, "Paris", ok="paris" in city.lower())
    assert "paris" in city.lower(), f"the call asked for {city!r}, the prompt asked for Paris"

    sky = fresh("violet")
    result = json.dumps({"city": "Paris", "temp_c": 17, "sky": sky})
    followup = wire(surface).followup(body, completion, [ToolOutput(call.id, result)])
    answer = check_basic_completion(
        client.post(surface.path, json_body=followup), surface, model, ingress
    )
    check_text_turn(answer, surface, model)
    assert_answer_contains(answer, [sky])


@pytest.mark.case_id("tools.auto_no_call")
def test_auto_without_need(client, surface, model, ingress):
    """Auto answers a prompt that needs no tool in text, without a call or call markup."""
    require_auto(model, ingress)
    body = wire(surface).request(
        model.public_id,
        "Reply with the single word: ok",
        [WEATHER],
        ToolChoice("auto"),
        tool_budget(model),
    )
    check_text_turn(post_tools(client, surface, ingress, body), surface, model)


@pytest.mark.case_id("tools.required")
def test_required_call(client, surface, model, ingress):
    """required (any on Messages) calls a tool even for a greeting."""
    choice = ToolChoice("required")
    body = wire(surface).request(
        model.public_id, "Hello there.", [WEATHER], choice, tool_budget(model)
    )
    completion = post_tools(client, surface, ingress, body)
    check_call_turn(completion, surface, model, [WEATHER], choice, names=["get_weather"])


@pytest.mark.case_id("tools.none")
def test_none_answers_in_text(client, surface, model, ingress):
    """none with tools offered returns answer text, even when the prompt wants a tool.

    Agent loops send none to force a final answer, so an empty message ends them with nothing.
    """
    body = wire(surface).request(
        model.public_id, ASK_WEATHER, [WEATHER], ToolChoice("none"), tool_budget(model)
    )
    check_text_turn(post_tools(client, surface, ingress, body), surface, model)


@pytest.mark.case_id("tools.named_second")
def test_named_second_tool(client, surface, model, ingress):
    """A named choice forces that tool: the second of two, and not the one the prompt wants."""
    tools, choice = [WEATHER, TIME], ToolChoice("named", ("get_time",))
    body = wire(surface).request(model.public_id, ASK_WEATHER, tools, choice, tool_budget(model))
    completion = post_tools(client, surface, ingress, body)
    check_call_turn(completion, surface, model, tools, choice, names=["get_time"])


@pytest.mark.surface("responses")
@pytest.mark.parametrize("allowed_mode", ["required", "auto"])
@pytest.mark.case_id("tools.allowed")
def test_allowed_tools(client, surface, model, ingress, allowed_mode):
    """allowed_tools restricts calls to the allowed tool: required mode calls it, and auto mode
    calls it or answers in text. A call to the tool the prompt asks for fails."""
    if allowed_mode == "auto":
        require_auto(model, ingress)
    tools = [WEATHER, TIME]
    choice = ToolChoice("allowed", ("get_time",), allowed_mode)
    body = wire(surface).request(model.public_id, ASK_WEATHER, tools, choice, tool_budget(model))
    completion = post_tools(client, surface, ingress, body)
    if allowed_mode == "auto" and not completion.tool_calls:
        check_text_turn(completion, surface, model)
        return
    check_call_turn(completion, surface, model, [TIME], choice, names=["get_time"])


@pytest.mark.surface("chat_completions")
@pytest.mark.case_id("tools.allowed_chat")
def test_allowed_tools_chat(client, surface, model, ingress):
    """Chat allowed_tools is honored, or refused with a 4xx envelope; ignoring it fails."""
    tools, choice = [WEATHER, TIME], ToolChoice("allowed", ("get_time",))
    body = wire(surface).request(model.public_id, ASK_WEATHER, tools, choice, tool_budget(model))
    exchange = client.post(surface.path, json_body=body)
    if served_or_refused(exchange, surface, ingress):
        completion = surface.parse_completion(exchange.response_body)
        check_call_turn(completion, surface, model, tools, choice, names=["get_time"])
