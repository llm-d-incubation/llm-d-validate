"""Tool definitions: names, descriptions, parameter schemas, strict mode and large tool lists.

Most cases force a call with `required`, so an engine that constrains arguments to the schema
must return valid ones, and the verdict depends on the definition alone.
"""

from __future__ import annotations

import json

import pytest

from ...assertions.tool_calls import call_arguments
from ...evidence import journal
from ...surfaces.tool_wire import ToolChoice, ToolDef
from ..conftest import check_completion
from .common import (
    ASK_WEATHER,
    TIME,
    WEATHER,
    check_call_turn,
    fresh,
    post_tools,
    require_auto,
    served_or_refused,
    stream_tools,
    tool_budget,
    wire,
)

pytestmark = [pytest.mark.surface_generic("tools"), pytest.mark.needs("tools")]

REQUIRED = ToolChoice("required")
ORDER_PROMPT = "Record this order with the record_order tool: {values}"

SCHEMA_FEATURES = {
    "nested": (
        {
            "type": "object",
            "properties": {
                "customer": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "address": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}, "zip": {"type": "string"}},
                            "required": ["city", "zip"],
                        },
                    },
                    "required": ["name", "address"],
                },
                "lines": {
                    "type": "array",
                    "minItems": 2,
                    "items": {
                        "type": "object",
                        "properties": {"sku": {"type": "string"}, "qty": {"type": "integer"}},
                        "required": ["sku", "qty"],
                    },
                },
            },
            "required": ["customer", "lines"],
        },
        "customer Ana Lima at 12 Rue Verte, Lyon 69001; two lines: sku A-1 qty 2, sku B-7 qty 1.",
    ),
    "enum_bounds": (
        {
            "type": "object",
            "properties": {
                "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
                "quantity": {"type": "integer", "minimum": 1, "maximum": 5},
                "ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "note": {"type": "string", "default": "none"},
            },
            "required": ["urgency", "quantity", "ratio"],
        },
        "urgency high, quantity 3, ratio 0.5.",
    ),
    "unions": (
        {
            "type": "object",
            "properties": {
                "contact": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {"email": {"type": "string"}},
                            "required": ["email"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {"phone": {"type": "string"}},
                            "required": ["phone"],
                            "additionalProperties": False,
                        },
                    ]
                },
                "amount": {"oneOf": [{"type": "integer"}, {"type": "string", "enum": ["all"]}]},
            },
            "required": ["contact", "amount"],
        },
        "contact by email ana@example.com, amount all.",
    ),
    "refs": (
        {
            "type": "object",
            "$defs": {
                "line": {
                    "type": "object",
                    "properties": {
                        "sku": {"type": "string"},
                        "qty": {"type": "integer", "minimum": 1},
                    },
                    "required": ["sku", "qty"],
                    "additionalProperties": False,
                }
            },
            "properties": {"lines": {"type": "array", "items": {"$ref": "#/$defs/line"}}},
            "required": ["lines"],
            "additionalProperties": False,
        },
        "two lines: sku A-1 qty 2, sku B-7 qty 1.",
    ),
    "closed": (
        {
            "type": "object",
            "properties": {"sku": {"type": "string"}, "qty": {"type": "integer"}},
            "required": ["sku", "qty"],
            "additionalProperties": False,
        },
        "sku A-1, qty 2.",
    ),
    # As model libraries generate it: titles everywhere, optional fields as a union with null
    # and a null default, and an enum behind a $ref with a sibling default.
    "generated": (
        {
            "$defs": {"Unit": {"enum": ["c", "f"], "title": "Unit", "type": "string"}},
            "properties": {
                "city": {"title": "City", "type": "string"},
                "days": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                    "title": "Days",
                },
                "unit": {"$ref": "#/$defs/Unit", "default": "c"},
            },
            "required": ["city"],
            "title": "RecordOrderArgs",
            "type": "object",
        },
        "city Lyon, days 3, unit f.",
    ),
    "string_formats": (
        {
            "type": "object",
            "properties": {
                "start": {"type": "string", "format": "date"},
                "email": {"type": "string", "format": "email"},
                "order_id": {"type": "string", "format": "uuid"},
                "code": {"type": "string", "pattern": "^[A-Z]{2}-[0-9]{4}$", "maxLength": 7},
            },
            "required": ["start", "email", "order_id", "code"],
            "additionalProperties": False,
        },
        "start 2026-10-09, email ana@example.com, order_id 3f0c9e1a-6a43-4c4e-9b7e-2f2b1f0b8a11, "
        "code AB-1234.",
    ),
    # Property names that are also schema keywords. A layer that strips keys such as `strict`
    # wherever they appear removes the property, and the required argument goes missing.
    "keyword_names": (
        {
            "type": "object",
            "properties": {
                "strict": {"type": "boolean"},
                "type": {"type": "string", "enum": ["express"]},
                "format": {"type": "string", "enum": ["pdf"]},
                "required": {"type": "integer"},
            },
            "required": ["strict", "type", "format", "required"],
            "additionalProperties": False,
        },
        "strict true, type express, format pdf, required 2.",
    ),
    # An older draft's header and `definitions`, as some schema converters still emit.
    "draft07": (
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "definitions": {
                "line": {
                    "type": "object",
                    "properties": {"sku": {"type": "string"}, "qty": {"type": "integer"}},
                    "required": ["sku", "qty"],
                    "additionalProperties": False,
                }
            },
            "properties": {"lines": {"type": "array", "items": {"$ref": "#/definitions/line"}}},
            "required": ["lines"],
            "additionalProperties": False,
        },
        "two lines: sku A-1 qty 2, sku B-7 qty 1.",
    ),
}


def _forced(client, surface, model, ingress, prompt, tools, *, stream=False):
    body = wire(surface).request(
        model.public_id, prompt, tools, REQUIRED, tool_budget(model), stream=stream
    )
    if stream:
        return stream_tools(client, surface, ingress, body)
    return post_tools(client, surface, ingress, body)


@pytest.mark.parametrize("feature", sorted(SCHEMA_FEATURES))
@pytest.mark.case_id("tools.schema_features")
def test_schema_features(client, surface, model, ingress, feature):
    """Each JSON Schema feature is accepted and the forced call's arguments satisfy it."""
    schema, values = SCHEMA_FEATURES[feature]
    # The schema is the subject, so the tool is strict: the fleet promises to honour it.
    tool = ToolDef("record_order", "Record a customer order.", schema, strict=True)
    prompt = ORDER_PROMPT.format(values=values)
    completion = _forced(client, surface, model, ingress, prompt, [tool])
    check_call_turn(completion, surface, model, [tool], REQUIRED, names=["record_order"])


BOOK = ToolDef(
    "book",
    "Book a trip.",
    {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "nights": {"type": "integer"},
            "guests": {"type": "array", "items": {"type": "string"}},
            "refundable": {"type": "boolean"},
            "extras": {
                "type": "object",
                "properties": {"breakfast": {"type": "boolean"}, "note": {"type": "string"}},
            },
        },
        "required": ["city", "nights", "guests", "refundable", "extras"],
    },
    strict=True,
)


@pytest.mark.parametrize("mode", ["json", "sse"])
@pytest.mark.case_id("tools.args_types")
def test_argument_types(client, surface, model, ingress, mode):
    """Integers, booleans, arrays, objects and non-ASCII text keep their JSON types and values."""
    prompt = (
        "Use the book tool: 3 nights in Zürich for Ana and Lei, refundable, with breakfast, "
        'and the note "窓側の席, ñ".'
    )
    completion = _forced(client, surface, model, ingress, prompt, [BOOK], stream=mode == "sse")
    [(_, arguments)] = check_call_turn(completion, surface, model, [BOOK], REQUIRED, names=["book"])
    extras = arguments["extras"] if isinstance(arguments["extras"], dict) else {}
    observed = {
        "nights": arguments["nights"],
        "refundable": arguments["refundable"],
        "zurich": "Zürich" in arguments["city"],
        "guests": len(arguments["guests"]),
        "breakfast": extras.get("breakfast"),
        "note": "窓側の席" in str(extras.get("note", "")),
    }
    expected = {
        "nights": 3,
        "refundable": True,
        "zurich": True,
        "guests": 2,
        "breakfast": True,
        "note": True,
    }
    ok = observed == expected and type(arguments["nights"]) is int
    journal.record("argument_values", observed, expected, ok=ok)
    assert ok, f"argument values changed in transit: {observed}, expected {expected}"


# A forced choice goes through a constrained decoder built from the schema, and an automatic
# one through the parser reading the model's own output, so each has its own empty-schema path.
NO_PARAMETER_CHOICES = {
    "required": REQUIRED,
    "named": ToolChoice("named", ("list_regions",)),
    "auto": ToolChoice("auto"),
}


@pytest.mark.parametrize("choice_mode", list(NO_PARAMETER_CHOICES))
@pytest.mark.parametrize("variant", ["minimal", "empty_properties"])
@pytest.mark.case_id("tools.no_parameters")
def test_no_parameters(client, surface, model, ingress, variant, choice_mode):
    """A tool without parameters is called with an empty JSON object under each choice."""
    if choice_mode == "auto":
        require_auto(model, ingress)
    choice = NO_PARAMETER_CHOICES[choice_mode]
    parameters = None if variant == "minimal" else {"type": "object", "properties": {}}
    tool = ToolDef("list_regions", "List every region. Takes no arguments.", parameters)
    prompt = "List every region using the list_regions tool."
    body = wire(surface).request(model.public_id, prompt, [tool], choice, tool_budget(model))
    completion = post_tools(client, surface, ingress, body)
    [(_, arguments)] = check_call_turn(
        completion, surface, model, [tool], choice, names=["list_regions"]
    )
    journal.record("no_parameter_arguments", arguments, {}, ok=arguments == {})
    assert arguments == {}, f"expected empty arguments, got {arguments!r}"


@pytest.mark.case_id("tools.no_description")
def test_no_description(client, surface, model, ingress):
    """Tools without the optional description are accepted and called by name."""
    tools = [
        ToolDef("get_weather", None, WEATHER.parameters),
        ToolDef("get_time", None, TIME.parameters),
    ]
    completion = _forced(client, surface, model, ingress, ASK_WEATHER, tools)
    check_call_turn(completion, surface, model, tools, REQUIRED, names=["get_weather"])


@pytest.mark.surface("chat_completions")
@pytest.mark.case_id("tools.legacy_functions")
def test_legacy_functions(client, surface, model, ingress):
    """The deprecated `functions` and `function_call` fields are honored with a function_call
    message, or refused with a 4xx. A stack that ignores them answers in text, and the client
    never gets its call."""
    function = {
        "name": WEATHER.name,
        "description": WEATHER.description,
        "parameters": WEATHER.parameters,
    }
    body = surface.minimal_body(
        model.public_id,
        tool_budget(model),
        ASK_WEATHER,
        functions=[function],
        function_call={"name": WEATHER.name},
    )
    exchange = client.post(surface.path, json_body=body)
    if not served_or_refused(exchange, surface, ingress):
        return
    choices = exchange.response_body.get("choices") or [{}]
    message = choices[0].get("message") or {}
    call = message.get("function_call") or {}
    try:
        arguments = json.loads(call.get("arguments") or "")
    except ValueError:
        arguments = None
    ok = call.get("name") == WEATHER.name and isinstance(arguments, dict)
    observed = call or {"content": message.get("content"), "tool_calls": message.get("tool_calls")}
    journal.record("legacy_function_call", observed, "a function_call to get_weather", ok=ok)
    assert ok, f"the legacy functions request was served without a function_call: {observed}"


@pytest.mark.case_id("tools.strict")
def test_strict(client, surface, model, ingress):
    """strict on a closed schema is accepted and the call's arguments satisfy the schema."""
    tool = ToolDef(
        "get_weather",
        "Current weather for a city.",
        {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        strict=True,
    )
    completion = _forced(client, surface, model, ingress, ASK_WEATHER, [tool])
    check_call_turn(completion, surface, model, [tool], REQUIRED, names=["get_weather"])


@pytest.mark.case_id("tools.many")
def test_many_tools(client, surface, model, ingress):
    """With 60 tools offered, auto calls the one the prompt needs."""
    require_auto(model, ingress)
    fillers = [
        ToolDef(
            f"tool_{index:02d}",
            f"Internal helper {index} for record keeping.",
            {
                "type": "object",
                "properties": {"id": {"type": "string"}, "limit": {"type": "integer"}},
            },
        )
        for index in range(58)
    ]
    # Past the first 32, where a layer that truncates long tool lists would cut.
    tools, choice = [*fillers[:40], WEATHER, *fillers[40:], TIME], ToolChoice("auto")
    journal.record("tools_offered", len(tools), 60, ok=len(tools) == 60)
    body = wire(surface).request(model.public_id, ASK_WEATHER, tools, choice, tool_budget(model))
    completion = post_tools(client, surface, ingress, body)
    check_call_turn(completion, surface, model, tools, choice, names=["get_weather"])


@pytest.mark.case_id("tools.name_charset")
def test_name_charset(client, surface, model, ingress):
    """A name at the API's length limit, of letters, digits, _ and -, comes back unchanged."""
    limit = wire(surface).name_limit
    name = (f"ns__Lookup-Tool_{fresh('v')}_" + "x" * limit)[:limit]
    tool = ToolDef(name, "Look up the current status.", {"type": "object", "properties": {}})
    journal.record("name_length", len(name), limit, ok=len(name) == limit)
    completion = _forced(client, surface, model, ingress, f"Call the {name} tool now.", [tool])
    check_call_turn(completion, surface, model, [tool], REQUIRED, names=[name])


@pytest.mark.case_id("tools.name_outside_spec")
def test_name_outside_spec(client, surface, model, ingress):
    """A name with dots, a colon and non-ASCII letters is refused clearly or returned unchanged."""
    name = "weather.lookup:v1-ünï"
    tool = ToolDef(name, "Current weather for a city.", WEATHER.parameters)
    body = wire(surface).request(
        model.public_id, f"Use the {name} tool for Paris.", [tool], REQUIRED, tool_budget(model)
    )
    exchange = client.post(surface.path, json_body=body)
    if served_or_refused(exchange, surface, ingress):
        completion = surface.parse_completion(exchange.response_body)
        check_call_turn(completion, surface, model, [tool], REQUIRED, names=[name])


@pytest.mark.case_id("tools.long_description")
def test_long_description(client, surface, model, ingress):
    """A description of about 6,000 characters reaches the model whole."""
    code = fresh("code")
    filler = (
        "This tool reads the archive index and returns matching record codes. It never "
        "changes data, and every call is logged for audit. "
    )
    description = filler * 45 + f"When you call this tool, set the code argument to {code}."
    tool = ToolDef(
        "archive_lookup",
        description,
        {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
    )
    journal.record("description_chars", len(description), "about 6000")
    prompt = "Look up the archive with the archive_lookup tool, as its description says."
    completion = _forced(client, surface, model, ingress, prompt, [tool])
    [(_, arguments)] = check_call_turn(
        completion, surface, model, [tool], REQUIRED, names=["archive_lookup"]
    )
    ok = arguments["code"] == code
    journal.record("description_tail_used", arguments["code"], code, ok=ok)
    assert ok, "the instruction at the end of the long description did not reach the model"


@pytest.mark.case_id("tools.duplicate_names")
def test_duplicate_names(client, surface, model, ingress):
    """Two tools with one name are refused clearly or answered with a well-formed call."""
    twin = ToolDef("get_weather", "Weather for a city, second definition.", WEATHER.parameters)
    body = wire(surface).request(
        model.public_id, ASK_WEATHER, [WEATHER, twin], REQUIRED, tool_budget(model)
    )
    exchange = client.post(surface.path, json_body=body)
    if served_or_refused(exchange, surface, ingress):
        completion = surface.parse_completion(exchange.response_body)
        check_call_turn(completion, surface, model, [WEATHER], REQUIRED, names=["get_weather"])


BIG = 9007199254740993


@pytest.mark.case_id("tools.big_int_argument")
def test_big_int_argument(client, surface, model, ingress):
    """An integer above 2^53 in a schema constant comes back exactly in the arguments."""
    tool = ToolDef(
        "open_record",
        "Open a record by its 64-bit id.",
        {
            "type": "object",
            "properties": {"record_id": {"type": "integer", "const": BIG}},
            "required": ["record_id"],
        },
    )
    prompt = f"Open record {BIG} with the open_record tool."
    completion = _forced(client, surface, model, ingress, prompt, [tool])
    check_completion(completion, surface, model)
    try:
        values = [call_arguments(call).get("record_id") for call in completion.tool_calls]
    except (ValueError, AttributeError) as exc:
        values = [f"unparseable: {exc}"]
    ok = values == [BIG]
    journal.record("big_int_argument", values, [BIG], ok=ok)
    assert ok, f"the 64-bit id came back as {values}, sent {BIG}"
    check_call_turn(completion, surface, model, [tool], REQUIRED, names=["open_record"])
