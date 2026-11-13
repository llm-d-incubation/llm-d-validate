"""Tool histories over several turns: a call that needs the previous result, and the forms a
replayed history may take on the wire."""

from __future__ import annotations

import json

import pytest

from ...assertions import fail
from ...assertions.messages import assert_messages_token_count
from ...assertions.structured_output import assert_structured_answer
from ...assertions.thinking import assert_thinking_blocks
from ...assertions.tool_calls import assert_answer_contains
from ...evidence import journal
from ...surfaces.tool_wire import PriorCall, ToolChoice, ToolDef, ToolOutput
from ..conftest import check_basic_completion, check_json_response
from .common import (
    CITY,
    TIME,
    WEATHER,
    call_id,
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

FIND_STATION = ToolDef("find_station", "Find the weather station id for a city.", CITY)
STATION_WEATHER = ToolDef(
    "station_weather",
    "Current weather reported by a weather station.",
    {"type": "object", "properties": {"station": {"type": "string"}}, "required": ["station"]},
)
CHAIN_PROMPT = (
    "What is the weather in Paris? First call find_station for Paris, then call station_weather "
    "with the station id that find_station returns, then answer with the exact sky value. One "
    "call at a time; never guess the id."
)


@pytest.mark.case_id("tools.chained_calls")
def test_chained_calls(client, surface, model, ingress):
    """A call after a result carries a value only that result held, and the last result
    reaches the answer: the loop every agent runs."""
    require_auto(model, ingress)
    tools, choice = [FIND_STATION, STATION_WEATHER], ToolChoice("auto")
    shape = wire(surface)
    body = shape.request(
        model.public_id, CHAIN_PROMPT, tools, choice, tool_budget(model), parallel=False
    )
    first = post_tools(client, surface, ingress, body)
    [(call, _)] = check_call_turn(first, surface, model, tools, choice, names=["find_station"])
    first_id = call.id

    station = fresh("ST")
    found = json.dumps({"city": "Paris", "station": station})
    body = shape.followup(body, first, [ToolOutput(call.id, found)])
    second = post_tools(client, surface, ingress, body)
    [(call, arguments)] = check_call_turn(
        second, surface, model, tools, choice, names=["station_weather"]
    )
    got = arguments.get("station")
    journal.record("chained_argument", got, station, ok=got == station)
    assert got == station, f"the second call asked for station {got!r}, the result held {station!r}"
    # Clients key results by call id across the whole conversation, so an id must not repeat.
    if call.id == first_id:
        fail("tool_call_ids_across_turns", [first_id, call.id], "a new id for each call")
    journal.record("ids_across_turns", [first_id, call.id], "a new id for each call", ok=True)

    sky = fresh("violet")
    report = json.dumps({"station": station, "sky": sky})
    body = shape.followup(body, second, [ToolOutput(call.id, report)])
    answer = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    check_text_turn(answer, surface, model)
    assert_answer_contains(answer, [sky])


WEATHER_PROMPT = (
    "What is the weather in Paris? Use get_weather, then answer with the exact sky value it "
    "returns, copied character for character."
)


def _replayed(surface, model, sky: str) -> dict:
    identity = call_id()
    return wire(surface).history(
        model.public_id,
        WEATHER_PROMPT,
        [WEATHER],
        [PriorCall(identity, "get_weather", {"city": "Paris"})],
        [ToolOutput(identity, json.dumps({"city": "Paris", "sky": sky}))],
        tool_budget(model),
    )


@pytest.mark.case_id("tools.history_without_tools")
def test_history_without_tools(client, surface, model, ingress):
    """A replayed call and result with no tools offered is answered from the result, or
    refused with a 4xx. The tools list is optional on every API, so clients send such turns."""
    sky = fresh("violet")
    body = _replayed(surface, model, sky)
    body.pop("tools")
    exchange = client.post(surface.path, json_body=body)
    if served_or_refused(exchange, surface, ingress):
        answer = check_basic_completion(exchange, surface, model, ingress)
        assert_answer_contains(answer, [sky])


@pytest.mark.surface("chat_completions")
@pytest.mark.parametrize("form", ["null", "empty", "omitted"])
@pytest.mark.case_id("tools.history_content_forms")
def test_history_content_forms(client, surface, model, ingress, form):
    """A replayed assistant call turn is accepted with content null, empty or left out, since
    the spec allows each, and the result reaches the answer."""
    sky = fresh("violet")
    body = _replayed(surface, model, sky)
    assistant = next(m for m in body["messages"] if m.get("tool_calls"))
    if form == "empty":
        assistant["content"] = ""
    elif form == "omitted":
        del assistant["content"]
    journal.record("assistant_content", assistant.get("content", "omitted"), form)
    answer = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    assert_answer_contains(answer, [sky])


# What a model can emit for a call: no argument text at all, or JSON cut short.
BAD_ARGUMENTS = {"empty": "", "malformed": '{"city": "Paris", "unit": '}


def _check_bad_arguments(client, surface, model, ingress, kind: str) -> None:
    sky = fresh("violet")
    identity = call_id()
    body = wire(surface).history(
        model.public_id,
        WEATHER_PROMPT,
        [WEATHER],
        [PriorCall(identity, "get_weather", BAD_ARGUMENTS[kind])],
        [ToolOutput(identity, json.dumps({"city": "Paris", "sky": sky}))],
        tool_budget(model),
    )
    journal.record("replayed_arguments", BAD_ARGUMENTS[kind], kind)
    exchange = client.post(surface.path, json_body=body)
    if served_or_refused(exchange, surface, ingress):
        answer = check_basic_completion(exchange, surface, model, ingress)
        assert_answer_contains(answer, [sky])


@pytest.mark.surface("chat_completions")
@pytest.mark.parametrize("kind", sorted(BAD_ARGUMENTS))
@pytest.mark.case_id("tools.history_bad_arguments")
def test_history_bad_arguments(client, surface, model, ingress, kind):
    """A replayed call whose argument text is empty or not valid JSON, as a model can emit it,
    is answered from its result or refused with a 4xx; a 5xx fails. Clients replay calls
    verbatim, including broken ones they answered with an error."""
    _check_bad_arguments(client, surface, model, ingress, kind)


@pytest.mark.surface("responses")
@pytest.mark.parametrize("kind", sorted(BAD_ARGUMENTS))
@pytest.mark.case_id("tools.history_bad_arguments_responses")
def test_history_bad_arguments_responses(client, surface, model, ingress, kind):
    """The same replayed broken call as a Responses function_call item."""
    _check_bad_arguments(client, surface, model, ingress, kind)


# Manual thinking needs budget_tokens of at least 1024 and max_tokens above it.
THINKING_BUDGET, THINKING_MAX_TOKENS = 1024, 4096


@pytest.mark.surface("messages")
@pytest.mark.case_id("tools.thinking_round_trip")
def test_thinking_round_trip(client, surface, model, ingress):
    """With thinking enabled, a call turn opens with a signed thinking block, and that turn
    replayed verbatim with its result is accepted and the result used. Agent clients with
    thinking on send every call turn back this way."""
    if "enabled" not in ingress.capabilities.messages_thinking:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare thinking mode 'enabled'")
    require_auto(model, ingress)
    tools, choice = [WEATHER, TIME], ToolChoice("auto")
    shape = wire(surface)
    body = shape.request(model.public_id, WEATHER_PROMPT, tools, choice, THINKING_MAX_TOKENS)
    # The native API does not allow a caller-selected temperature with thinking enabled.
    body.pop("temperature", None)
    body["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}
    completion = post_tools(client, surface, ingress, body)
    assert_thinking_blocks(completion, "enabled")
    [(call, _)] = check_call_turn(completion, surface, model, tools, choice, names=["get_weather"])

    sky = fresh("violet")
    result = json.dumps({"city": "Paris", "sky": sky})
    followup = shape.followup(body, completion, [ToolOutput(call.id, result)])
    answer = check_basic_completion(
        client.post(surface.path, json_body=followup), surface, model, ingress
    )
    check_text_turn(answer, surface, model)
    assert_answer_contains(answer, [sky])


WEATHER_ANSWER = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "sky": {"type": "string"}},
    "required": ["city", "sky"],
    "additionalProperties": False,
}


@pytest.mark.surface_generic("tools", "structured_output")
@pytest.mark.needs("tools", "structured_output")
@pytest.mark.case_id("tools.with_output_format")
def test_with_output_format(client, surface, model, ingress):
    """With a JSON Schema output format and tools in one request, the model still calls the
    tool it needs, and the answer after the result matches the schema.

    Agent frameworks send both when they want a typed final answer. Constraining the whole
    output to the answer schema from the first token leaves the model no way to call a tool.
    """
    require_auto(model, ingress)
    tools, choice = [WEATHER, TIME], ToolChoice("auto")
    shape = wire(surface)
    budget = tool_budget(model)
    body = surface.structured_output_body(model.public_id, WEATHER_ANSWER, budget, WEATHER_PROMPT)
    offered = shape.request(model.public_id, WEATHER_PROMPT, tools, choice, budget)
    body.update({key: value for key, value in offered.items() if key not in body})
    completion = post_tools(client, surface, ingress, body)
    [(call, _)] = check_call_turn(completion, surface, model, tools, choice, names=["get_weather"])

    sky = fresh("violet")
    result = json.dumps({"city": "Paris", "temp_c": 17, "sky": sky})
    followup = shape.followup(body, completion, [ToolOutput(call.id, result)])
    answer = check_basic_completion(
        client.post(surface.path, json_body=followup), surface, model, ingress
    )
    assert_structured_answer(answer, WEATHER_ANSWER, model)
    got = json.loads(answer.text).get("sky")
    journal.record("answer_sky", got, sky, ok=got == sky)
    assert got == sky, f"the answer reports sky {got!r}, the result held {sky!r}"


# The fields count_tokens takes; generation fields such as max_tokens are left out.
COUNTED_FIELDS = ("model", "system", "messages", "tools", "tool_choice")


@pytest.mark.surface("messages")
@pytest.mark.needs("tools", "count_tokens")
@pytest.mark.case_id("tools.count_tokens")
def test_token_count_with_tools(client, surface, model, ingress):
    """count_tokens counts the tool list and a replayed call and result. Clients budget their
    context with this count, so one that skips tools lets the real request overflow."""
    replayed = _replayed(surface, model, fresh("violet"))
    counted = {key: value for key, value in replayed.items() if key in COUNTED_FIELDS}
    bodies = {
        "prompt": {**counted, "messages": counted["messages"][:1], "tools": None},
        "prompt_and_tools": {**counted, "messages": counted["messages"][:1]},
        "history_and_tools": counted,
    }
    counts = {}
    for name, body in bodies.items():
        body = {key: value for key, value in body.items() if value is not None}
        exchange = client.post(surface.count_tokens_path, json_body=body)
        counts[name] = assert_messages_token_count(check_json_response(exchange, ingress))
    ok = counts["prompt"] < counts["prompt_and_tools"] < counts["history_and_tools"]
    journal.record("count_tokens_grow", counts, "prompt < with tools < with call history", ok=ok)
    assert ok, f"count_tokens does not count tools or tool history: {counts}"
