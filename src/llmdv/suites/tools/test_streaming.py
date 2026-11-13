"""Streamed tool calls: each API's fragment grammar, and agreement with the non-streamed call."""

from __future__ import annotations

import uuid

import pytest

from ...assertions import fail
from ...assertions.chat_choices import assert_choice_indices, assert_choice_stream
from ...assertions.chat_tools import assert_chat_tool_deltas
from ...assertions.tool_calls import assert_chat_tool_openers
from ...evidence import journal
from ...surfaces.tool_wire import ToolChoice, ToolDef
from ..conftest import budget_for, check_json_response, check_sse_response
from .common import (
    ASK_WEATHER,
    TIME,
    WEATHER,
    check_call_turn,
    post_tools,
    require_auto,
    stream_tools,
    tool_budget,
    wire,
)

pytestmark = [pytest.mark.surface_generic("tools"), pytest.mark.needs("tools")]


@pytest.mark.case_id("tools.auto_stream")
def test_auto_stream(client, surface, model, ingress):
    """An automatic call streams in the API's own fragment grammar and assembles cleanly."""
    require_auto(model, ingress)
    tools, choice = [WEATHER, TIME], ToolChoice("auto")
    body = wire(surface).request(
        model.public_id, ASK_WEATHER, tools, choice, tool_budget(model), stream=True
    )
    completion = stream_tools(client, surface, ingress, body)
    check_call_turn(completion, surface, model, tools, choice, names=["get_weather"])


def _ticket_tool(marker: str) -> ToolDef:
    return ToolDef(
        "file_ticket",
        "File a support ticket.",
        {
            "type": "object",
            "properties": {
                "ticket": {"type": "string", "enum": [marker]},
                "severity": {"type": "integer", "enum": [3]},
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["network", "disk"]},
                    "minItems": 2,
                    "maxItems": 2,
                    "uniqueItems": True,
                },
                "urgent": {"type": "boolean", "enum": [False]},
                "summary": {"type": "string"},
            },
            "required": ["ticket", "severity", "tags", "urgent", "summary"],
            "additionalProperties": False,
        },
    )


@pytest.mark.case_id("tools.stream_matches_json")
def test_stream_matches_json(client, surface, model, ingress):
    """The same forced call, streamed and not, yields the same parsed arguments."""
    tool, choice = _ticket_tool(uuid.uuid4().hex[:12]), ToolChoice("required")
    prompt = (
        "File the ticket with the file_ticket tool, tagged network and disk, with the summary "
        '"disk full".'
    )
    arguments = {}
    for mode in ("json", "sse"):
        body = wire(surface).request(
            model.public_id, prompt, [tool], choice, tool_budget(model), stream=mode == "sse"
        )
        if mode == "sse":
            completion = stream_tools(client, surface, ingress, body)
        else:
            completion = post_tools(client, surface, ingress, body)
        [(_, value)] = check_call_turn(
            completion, surface, model, [tool], choice, names=["file_ticket"]
        )
        arguments[mode] = {**value, "tags": sorted(value["tags"])}
    ok = arguments["json"] == arguments["sse"]
    journal.record("streamed_equals_json", arguments["sse"], arguments["json"], ok=ok)
    assert ok, f"streamed arguments {arguments['sse']} differ from {arguments['json']}"


@pytest.mark.case_id("tools.named_stream")
def test_named_stream(client, surface, model, ingress):
    """A named choice streams as a call to that tool, in the API's own fragment grammar.

    Engines decode a forced call on a path of its own; a stream that carries the arguments as
    answer text leaves the client with no call to run.
    """
    tools, choice = [WEATHER, TIME], ToolChoice("named", ("get_time",))
    body = wire(surface).request(
        model.public_id, ASK_WEATHER, tools, choice, tool_budget(model), stream=True
    )
    completion = stream_tools(client, surface, ingress, body)
    check_call_turn(completion, surface, model, tools, choice, names=["get_time"])


WRITE_FILE = ToolDef(
    "write_file",
    "Write a text file.",
    {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
        "additionalProperties": False,
    },
)
# Source text whose JSON form is full of escapes: backslashes that read as escape sequences if a
# parser copies them raw, escaped quotes, a closing quote and brace inside a string, and non-ASCII.
CODE = "\n".join(
    [
        "import json",
        "import re",
        "",
        'ROOT = r"C:\\temp\\new"',
        'PATTERN = re.compile(r"\\d+\\.\\d+")',
        "CLOSERS = '\"}'",
        'GREETING = "say \\"hi\\" to Zo\u00eb"',
        "",
        "",
        "def main():",
        '    data = {"path": ROOT, "items": [1, 2, 3]}',
        "    print(json.dumps(data))",
        "    print(GREETING, CLOSERS)",
        "",
        "",
        'if __name__ == "__main__":',
        "    main()",
    ]
)
CODE_PROMPT = (
    'Use the write_file tool to save the file with path "app/main.py". Its content must be '
    "exactly the text between the two lines of five equals signs, copied character for "
    "character, keeping every backslash, quote and line break:\n=====\n" + CODE + "\n====="
)
CODE_EXPECTED = f"{CODE}, optionally ending with one newline"


@pytest.mark.parametrize("mode", ["json", "sse"])
@pytest.mark.case_id("tools.code_arguments")
def test_code_arguments(client, surface, model, ingress, mode):
    """Source code passed as an argument keeps every backslash, quote and line break.

    Coding agents write files through tool calls, and the call's arguments are the model's own
    text turned into a JSON string by the tool parser, which is where escapes break. One newline
    ending the last line is a fair reading of "keeping every line break", so it is allowed; any
    other difference fails.
    """
    require_auto(model, ingress)
    tools, choice = [WRITE_FILE], ToolChoice("auto")
    body = wire(surface).request(
        model.public_id,
        CODE_PROMPT,
        tools,
        choice,
        budget_for(model, floor=1024),
        stream=mode == "sse",
    )
    if mode == "sse":
        completion = stream_tools(client, surface, ingress, body)
    else:
        completion = post_tools(client, surface, ingress, body)
    [(_, arguments)] = check_call_turn(
        completion, surface, model, tools, choice, names=["write_file"]
    )
    content = arguments.get("content")
    if content not in (CODE, CODE + "\n"):
        fail("code_content_exact", content, CODE_EXPECTED)
    journal.record("code_content_exact", content, CODE_EXPECTED, ok=True)


CHOICES = 2


@pytest.mark.surface("chat_completions")
@pytest.mark.parametrize("mode", ["json", "sse"])
@pytest.mark.case_id("tools.n_choices")
def test_n_choices(client, surface, model, ingress, mode):
    """With n 2, each choice carries its own valid call with its own id.

    A stream needs one parser state per choice; one shared between choices loses a choice's
    call or mixes fragments across choices.
    """
    if "n" not in ingress.capabilities.chat_options:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare Chat n")
    require_auto(model, ingress)
    tools, choice = [WEATHER, TIME], ToolChoice("auto")
    body = wire(surface).request(
        model.public_id, ASK_WEATHER, tools, choice, tool_budget(model), stream=mode == "sse"
    )
    # vLLM serves several choices only with sampling.
    body.update(n=CHOICES, temperature=1)
    if mode == "sse":
        exchange, transcript = client.stream(surface.path, json_body=body)
        check_sse_response(exchange, ingress)
        frames = [event.json() for event in transcript.data_events]
        assert_choice_stream(frames, CHOICES, model.public_id)
        for index in range(CHOICES):
            own = [
                {**frame, "choices": [part]}
                for frame in frames
                for part in frame["choices"]
                if part.get("index") == index
            ]
            assert_chat_tool_deltas(own)
            assert_chat_tool_openers(own)
        completions = surface.assemble_choices(frames, CHOICES)
    else:
        response = check_json_response(client.post(surface.path, json_body=body), ingress)
        assert_choice_indices(response.get("choices"), CHOICES)
        completions = surface.parse_completions(response)
    ids = []
    for index, completion in enumerate(completions):
        journal.record("checking_choice", index)
        calls = check_call_turn(completion, surface, model, tools, choice, names=["get_weather"])
        ids += [call.id for call, _ in calls]
    if len(set(ids)) != len(ids):
        fail("tool_call_ids_across_choices", ids, "no id shared between choices")
    journal.record("ids_across_choices", ids, "no id shared between choices", ok=True)
