"""Tool results in every form a client sends, replayed into a conversation the case writes.

Each case writes the prior call itself, so one request per case suffices, and a fresh value in
the result is the only place the answer can take it from.
"""

from __future__ import annotations

import json
import random

import pytest

from ...assertions import assert_client_error, assert_error_envelope, fail, tolerates
from ...assertions.context import prompt_size
from ...assertions.images import assert_image_read
from ...assertions.structured_output import assert_structured_answer
from ...assertions.tool_calls import assert_answer_contains
from ...evidence import journal
from ...surfaces.images import encode_png, striped_image
from ...surfaces.tool_wire import PriorCall, ToolDef, ToolOutput
from ..conftest import check_basic_completion
from .common import TIME, WEATHER, call_id, fresh, served_or_refused, tool_budget, wire

pytestmark = [pytest.mark.surface_generic("tools"), pytest.mark.needs("tools")]

WEATHER_PROMPT = (
    "What is the weather in Paris? Use get_weather, then answer with the exact sky value it "
    "returns, copied character for character."
)


def _weather_turn(client, surface, model, output: ToolOutput, prompt=WEATHER_PROMPT):
    calls = [PriorCall(output.call_id or call_id(), "get_weather", {"city": "Paris"})]
    body = wire(surface).history(
        model.public_id, prompt, [WEATHER, TIME], calls, [output], tool_budget(model)
    )
    return client.post(surface.path, json_body=body)


def _answer(client, surface, model, ingress, output: ToolOutput, prompt=WEATHER_PROMPT):
    exchange = _weather_turn(client, surface, model, output, prompt)
    return _check_answer(exchange, surface, model, ingress)


def _check_answer(exchange, surface, model, ingress):
    # The shared contract judges leaked call markup in the answer.
    return check_basic_completion(exchange, surface, model, ingress)


@pytest.mark.case_id("tools.result_parts")
def test_result_parts(client, surface, model, ingress):
    """A result sent as a list of text parts reaches the model."""
    sky = fresh("violet")
    parts = ("Weather report for Paris follows.", json.dumps({"temp_c": 17, "sky": sky}))
    answer = _answer(client, surface, model, ingress, ToolOutput(call_id(), parts))
    assert_answer_contains(answer, [sky])


@pytest.mark.case_id("tools.result_large")
def test_result_large(client, surface, model, ingress):
    """A JSON result of about 20,000 characters reaches the model whole."""
    sky = fresh("violet")
    stations = [
        {"station": f"S-{index:03d}", "city": f"Town {index}", "sky": "clear", "temp_c": 20}
        for index in range(320)
    ]
    stations.insert(250, {"station": "S-PAR", "city": "Paris", "sky": sky, "temp_c": 17})
    result = json.dumps({"stations": stations})
    journal.record("result_chars", len(result), "about 20000")
    prompt = (
        "What is the weather in Paris? Use get_weather; it returns every station. Answer with "
        "the exact sky value of the Paris station, copied character for character."
    )
    answer = _answer(client, surface, model, ingress, ToolOutput(call_id(), result), prompt)
    assert_answer_contains(answer, [sky])


@pytest.mark.case_id("tools.result_unicode")
def test_result_unicode(client, surface, model, ingress):
    """A non-ASCII result arrives unchanged and is quoted back exactly."""
    sky = f"{fresh('nuage')}-ünïcødé-雷雨-ñandú"
    result = json.dumps({"city": "Paris", "sky": sky}, ensure_ascii=False)
    answer = _answer(client, surface, model, ingress, ToolOutput(call_id(), result))
    assert_answer_contains(answer, [sky])


@pytest.mark.case_id("tools.result_empty")
def test_result_empty(client, surface, model, ingress):
    """An empty result is accepted and the next turn completes."""
    remove = ToolDef(
        "delete_file",
        "Delete a file. Returns nothing on success.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    identity = call_id()
    body = wire(surface).history(
        model.public_id,
        "Delete the file a.txt with delete_file, then confirm in one short sentence.",
        [remove],
        [PriorCall(identity, "delete_file", {"path": "a.txt"})],
        [ToolOutput(identity, "")],
        tool_budget(model),
    )
    _check_answer(client.post(surface.path, json_body=body), surface, model, ingress)


@pytest.mark.case_id("tools.result_multiple")
def test_result_multiple(client, surface, model, ingress):
    """Two results, sent in the reverse order of their calls, both reach the answer."""
    sky, clock = fresh("violet"), fresh("time")
    weather_id, time_id = call_id(), call_id()
    body = wire(surface).history(
        model.public_id,
        "What are the weather and the local time in Paris? Use get_weather and get_time, then "
        "answer with the exact sky value and the exact time value they return.",
        [WEATHER, TIME],
        [
            PriorCall(weather_id, "get_weather", {"city": "Paris"}),
            PriorCall(time_id, "get_time", {"city": "Paris"}),
        ],
        [
            ToolOutput(time_id, json.dumps({"city": "Paris", "time": clock})),
            ToolOutput(weather_id, json.dumps({"city": "Paris", "sky": sky})),
        ],
        tool_budget(model),
    )
    answer = _check_answer(client.post(surface.path, json_body=body), surface, model, ingress)
    assert_answer_contains(answer, [sky, clock])


@pytest.mark.case_id("tools.result_partial")
def test_result_partial(client, surface, model, ingress):
    """A turn that answers one of two calls, as a client does when a call was cancelled, is
    answered from the result it has or refused with a 4xx; a 5xx fails."""
    sky = fresh("violet")
    weather_id, time_id = call_id(), call_id()
    body = wire(surface).history(
        model.public_id,
        WEATHER_PROMPT,
        [WEATHER, TIME],
        [
            PriorCall(weather_id, "get_weather", {"city": "Paris"}),
            PriorCall(time_id, "get_time", {"city": "Paris"}),
        ],
        [ToolOutput(weather_id, json.dumps({"city": "Paris", "sky": sky}))],
        tool_budget(model),
    )
    exchange = client.post(surface.path, json_body=body)
    if served_or_refused(exchange, surface, ingress):
        answer = _check_answer(exchange, surface, model, ingress)
        assert_answer_contains(answer, [sky])


def _orphan(client, surface, model, ingress, output: ToolOutput) -> None:
    """A result that answers no issued call gets the declared outcome."""
    prior = PriorCall(call_id(), "get_weather", {"city": "Paris"})
    body = wire(surface).history(
        model.public_id, WEATHER_PROMPT, [WEATHER], [prior], [output], tool_budget(model)
    )
    exchange = client.post(surface.path, json_body=body)
    assert_client_error(exchange, ingress, "orphan_tool_result")
    if tolerates(ingress, "orphan_tool_result"):
        _check_answer(exchange, surface, model, ingress)
        journal.record("orphan_result_served", True, f"declared by {ingress.id}", ok=True)
        return
    assert_error_envelope(exchange, surface, ingress, "orphan_tool_result")


@pytest.mark.case_id("tools.result_unknown_id")
def test_result_unknown_id(client, surface, model, ingress):
    """A result whose id names no issued call is refused, unless the ingress declares it served."""
    _orphan(client, surface, model, ingress, ToolOutput(call_id(), '{"sky": "clear"}'))


@pytest.mark.case_id("tools.result_missing_id")
def test_result_missing_id(client, surface, model, ingress):
    """A result without its call id is refused, unless the ingress declares it served."""
    _orphan(client, surface, model, ingress, ToolOutput(None, '{"sky": "clear"}'))


@pytest.mark.surface("messages")
@pytest.mark.case_id("tools.result_is_error")
def test_result_is_error(client, surface, model, ingress):
    """is_error reaches the model: the same turn renders more input with it set."""
    identity = call_id()
    counts = {}
    for flag in (None, True):
        body = wire(surface).history(
            model.public_id,
            WEATHER_PROMPT,
            [WEATHER],
            [PriorCall(identity, "get_weather", {"city": "Paris"})],
            [ToolOutput(identity, "Station offline.", is_error=flag)],
            tool_budget(model),
        )
        completion = _check_answer(
            client.post(surface.path, json_body=body), surface, model, ingress
        )
        # The whole prompt, cached or not: Messages input_tokens is only the uncached tail,
        # which a warmer second request shrinks whether or not is_error was rendered.
        counts["is_error" if flag else "without"] = prompt_size(surface, completion)
    ok = counts["is_error"] > counts["without"]
    journal.record("is_error_input_tokens", counts, "more input tokens with is_error true", ok=ok)
    assert ok, f"is_error did not change the rendered input: {counts}"


def _noise_png(side: int = 64) -> bytes:
    """An incompressible RGB image: its base64 text is about 16 KB if a layer inlines it."""
    noise = random.Random(7)
    rows = b"".join(
        b"\x00" + bytes(noise.getrandbits(8) for _ in range(side * 3)) for _ in range(side)
    )
    return encode_png(side, side, rows)


# An image is a few hundred tokens of vision input or a clear refusal. A layer that inlines its
# base64 as text produces many thousands.
IMAGE_INPUT_TOKEN_CEILING = 2000


def _check_image_result(client, surface, model, ingress) -> None:
    output = ToolOutput(call_id(), "Screenshot attached.", image_png=_noise_png())
    exchange = _weather_turn(client, surface, model, output)
    if not served_or_refused(exchange, surface, ingress):
        return
    completion = _check_answer(exchange, surface, model, ingress)
    tokens = prompt_size(surface, completion)
    if tokens >= IMAGE_INPUT_TOKEN_CEILING:
        fail("image_input_tokens", tokens, f"below {IMAGE_INPUT_TOKEN_CEILING}")
    journal.record("image_input_tokens", tokens, f"below {IMAGE_INPUT_TOKEN_CEILING}", ok=True)


@pytest.mark.surface("messages")
@pytest.mark.case_id("tools.result_image")
def test_result_image(client, surface, model, ingress):
    """An image in a tool_result is served as an image or refused; inlined text fails."""
    _check_image_result(client, surface, model, ingress)


@pytest.mark.surface("responses")
@pytest.mark.case_id("tools.result_image_responses")
def test_result_image_responses(client, surface, model, ingress):
    """An image in a function_call_output is served as an image or refused; text fails."""
    _check_image_result(client, surface, model, ingress)


SCREENSHOT = ToolDef(
    "take_screenshot",
    "Capture the screen. Returns the capture as an image.",
    {"type": "object", "properties": {}},
)


def _check_image_read(client, surface, model, ingress) -> None:
    """The model answers from the image a result carried, as a client reading a screenshot needs."""
    picture = striped_image()
    identity = call_id()
    body = wire(surface).history(
        model.public_id,
        f"Take a screenshot with take_screenshot. The screenshot {picture.question}",
        [SCREENSHOT],
        [PriorCall(identity, "take_screenshot", {})],
        [ToolOutput(identity, "Screenshot attached.", image_png=picture.png)],
        tool_budget(model),
    )
    answer = _check_answer(client.post(surface.path, json_body=body), surface, model, ingress)
    assert_image_read(answer.text, picture.colours)


@pytest.mark.surface("messages")
@pytest.mark.needs("tools", "vision")
@pytest.mark.case_id("tools.result_image_read")
def test_result_image_read(client, surface, model, ingress):
    """An image in a tool_result is read: the answer names what only the image shows."""
    _check_image_read(client, surface, model, ingress)


@pytest.mark.surface("responses")
@pytest.mark.needs("tools", "vision")
@pytest.mark.case_id("tools.result_image_read_responses")
def test_result_image_read_responses(client, surface, model, ingress):
    """An image in a function_call_output is read: the answer names what only it shows."""
    _check_image_read(client, surface, model, ingress)


@pytest.mark.case_id("tools.result_then_text")
def test_result_then_text(client, surface, model, ingress):
    """User text after a result reaches the model with it, as clients send reminders there."""
    sky, code = fresh("violet"), fresh("code")
    identity = call_id()
    body = wire(surface).history(
        model.public_id,
        WEATHER_PROMPT,
        [WEATHER],
        [PriorCall(identity, "get_weather", {"city": "Paris"})],
        [ToolOutput(identity, json.dumps({"city": "Paris", "sky": sky}))],
        tool_budget(model),
        after=f"Also end your answer with the code {code}.",
    )
    answer = _check_answer(client.post(surface.path, json_body=body), surface, model, ingress)
    assert_answer_contains(answer, [sky, code])


@pytest.mark.case_id("tools.history_with_text")
def test_history_with_text(client, surface, model, ingress):
    """A replayed assistant turn with text beside its calls is accepted and its result used."""
    sky = fresh("violet")
    identity = call_id()
    body = wire(surface).history(
        model.public_id,
        WEATHER_PROMPT,
        [WEATHER],
        [PriorCall(identity, "get_weather", {"city": "Paris"})],
        [ToolOutput(identity, json.dumps({"city": "Paris", "sky": sky}))],
        tool_budget(model),
        preface="I will check the weather in Paris first.",
    )
    answer = _check_answer(client.post(surface.path, json_body=body), surface, model, ingress)
    assert_answer_contains(answer, [sky])


@pytest.mark.surface("messages")
@pytest.mark.parametrize("kind", ["search_result", "document"])
@pytest.mark.case_id("tools.result_blocks")
def test_result_blocks(client, surface, model, ingress, kind):
    """A search result or document block in a tool_result is served and used, or refused."""
    sky = fresh("violet")
    block = wire(surface).result_block(kind, "Paris weather", f"Paris sky today: {sky}.")
    exchange = _weather_turn(client, surface, model, ToolOutput(call_id(), blocks=(block,)))
    if served_or_refused(exchange, surface, ingress):
        answer = _check_answer(exchange, surface, model, ingress)
        assert_answer_contains(answer, [sky])


MARKUP_SUMMARY = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


def _closing(tag: str) -> str:
    """The closing form of a call tag, for notes that quote the markup as a parser would see it."""
    if tag.startswith("<") and tag.endswith(">"):
        return "</" + tag[1:]
    return ""


def markup_notes(markup: tuple[str, ...]) -> str:
    """A tool result that quotes the model's own call markup, as a file about the parser would."""
    quoted = "; ".join(f"{tag}...{_closing(tag)}" if _closing(tag) else tag for tag in markup)
    return f"# Parser notes\n\nEvery function call is written as {quoted}.\n"


@pytest.mark.needs("structured_output")
@pytest.mark.case_id("tools.result_quotes_markup")
def test_result_quotes_markup_then_schema(client, surface, model, ingress):
    """A result that quotes the model's own call markup, followed by a JSON Schema answer, is
    served, and the answer matches the schema.

    Agents feed tool output that shows the parser's tags (a file about tools, a log) and then ask
    for a typed answer. A reasoning model repeats the tag while thinking; a grammar that treats
    that token as the start of its output must not fail the request (a 500) or empty it.
    """
    markup = tuple(model.tools.markup)
    if not markup:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no model.tools.markup")
    notes = markup_notes(markup)
    journal.record("markup_quoted", list(markup))
    prompt = (
        "What is the weather in Paris? Use get_weather; it returns the station notes. Then "
        "summarise the notes in one sentence that quotes the exact tag syntax they describe."
    )
    budget = tool_budget(model)
    prior = call_id()
    body = wire(surface).history(
        model.public_id,
        prompt,
        [WEATHER, TIME],
        [PriorCall(prior, "get_weather", {"city": "Paris"})],
        [ToolOutput(prior, notes)],
        budget,
    )
    formatted = surface.structured_output_body(model.public_id, MARKUP_SUMMARY, budget, prompt)
    body.update({key: value for key, value in formatted.items() if key not in body})
    exchange = client.post(surface.path, json_body=body)
    # check_basic_completion understands a declared throttle before it requires the 200. The
    # prompt asks the summary to quote the tags, so markup in this answer is no leak.
    completion = check_basic_completion(exchange, surface, model, ingress, quotes_markup=True)
    assert_structured_answer(completion, MARKUP_SUMMARY, model)
