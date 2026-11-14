"""Byte fidelity: what the client sent is what the model sees, through every hop.

A hop that decodes a request into generic values and encodes it again changes it silently:
integers above 2^53 become floats, object keys come out sorted, escapes are rewritten. Each
response stays well formed, so only a comparison of what was sent with what was rendered or
generated can see it.

`evolution.big_integer` sends one tool schema twice, once with an integer above 2^53 and once
with that integer's float spelling. The two render differently only if every hop kept the
digits, so equal input token counts mean some hop converted the integer. No model judgement is
involved. `evolution.big_integer_history` does the same inside a prior `tool_use` input on
Messages, the one API that replays a tool call's input as a JSON object. `evolution.key_order`
asks for structured output whose schema lists its properties out of alphabetical order;
grammar-constrained decoding emits them in the order the engine received, so sorted keys mean
some hop sorted the schema. `evolution.unicode_escapes` sends one
body spelled once as UTF-8 and once with every non-ASCII character escaped, surrogate pairs
included; both decode to the same request, so their input token counts must be equal.

Each probe is opt-in under `capabilities.byte_fidelity` and skips as `undeclared` otherwise.
"""

from __future__ import annotations

import json

import pytest

from ...assertions import fail
from ...assertions.structured_output import assert_structured_answer
from ...evidence import journal
from ...surfaces import variants
from ...surfaces.base import DEFAULT_PROMPT, ToolSpec
from ..conftest import budget_for, check_basic_completion
from ..field_options.probe import schema_probe
from .helpers import input_tokens

pytestmark = pytest.mark.surface_generic

# Above 2^64, so a float64 round trip cannot keep it; the float spelling is what that writes.
BIG_INTEGER = 12345678901234567890123
BIG_AS_FLOAT = 1.2345678901234568e22
# Astral-plane characters travel as surrogate pairs when escaped; the rest cover CJK, Arabic,
# a combining mark and an escaped solidus.
UNICODE_TEXT = (
    "Echo nothing. \u6f22\u5b57 \u0627\u0644\u0639\u0631\u0628\u064a\u0629 e\u0301 "
    "\U0001f680 \U0001d11e a/b"
)


def _declared(ingress, probe: str) -> None:
    if probe not in ingress.capabilities.byte_fidelity:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare byte_fidelity {probe}")


@pytest.mark.surface_generic("tools")
@pytest.mark.needs("tools")
@pytest.mark.case_id("evolution.big_integer")
def test_big_integer_reaches_the_model(client, surface, model, ingress):
    _declared(ingress, "big_integer")

    def body(bound):
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer", "maximum": bound}},
            "required": ["count"],
        }
        tool = ToolSpec("llmdv_set_count", "Set the counter.", schema)
        prompt = f"Call {tool.name} once. {DEFAULT_PROMPT}"
        return variants.forced_tools_body(
            surface, model.public_id, budget_for(model), prompt, [tool]
        )

    exact = input_tokens(client, surface, model, ingress, json_body=body(BIG_INTEGER))
    rounded = input_tokens(client, surface, model, ingress, json_body=body(BIG_AS_FLOAT))
    journal.record(
        "input_tokens_integer_vs_float",
        [exact, rounded],
        "different: the integer's digits reached the engine",
        ok=exact != rounded,
    )
    if exact == rounded:
        fail(
            "big_integer_digits",
            f"{BIG_INTEGER} rendered like {BIG_AS_FLOAT!r} ({exact} tokens)",
            "a hop that keeps integers above 2^53 exact",
        )


@pytest.mark.surface("messages")
@pytest.mark.needs("tools")
@pytest.mark.case_id("evolution.big_integer_history")
def test_big_integer_in_a_replayed_tool_call(client, surface, model, ingress):
    """The same check on a prior tool call's input, which a client replays every turn."""
    _declared(ingress, "big_integer_history")
    budget = budget_for(model)
    exact_body = variants.tool_history_body(surface, model.public_id, budget, BIG_INTEGER)
    rounded_body = variants.tool_history_body(surface, model.public_id, budget, BIG_AS_FLOAT)
    exact = input_tokens(client, surface, model, ingress, json_body=exact_body)
    rounded = input_tokens(client, surface, model, ingress, json_body=rounded_body)
    journal.record(
        "input_tokens_integer_vs_float",
        [exact, rounded],
        "different: the integer's digits reached the engine",
        ok=exact != rounded,
    )
    if exact == rounded:
        fail(
            "big_integer_digits",
            f"{BIG_INTEGER} in a tool_use input rendered like {BIG_AS_FLOAT!r} ({exact} tokens)",
            "a hop that keeps integers above 2^53 exact",
        )


@pytest.mark.surface_generic("structured_output")
@pytest.mark.needs("structured_output")
@pytest.mark.case_id("evolution.key_order")
def test_schema_key_order_reaches_the_answer(client, surface, model, ingress):
    _declared(ingress, "key_order")
    schema = schema_probe()
    declared = list(schema["properties"])
    if declared == sorted(declared):
        raise AssertionError("llmdv defect: the probe schema must list its keys unsorted")
    body = surface.structured_output_body(
        model.public_id,
        schema,
        max_tokens=budget_for(model, floor=256),
        prompt="Return a JSON object matching the supplied output schema. Set count to 1.",
    )
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    assert_structured_answer(completion, schema, model)
    observed = [key for key, _ in json.loads(completion.text, object_pairs_hook=list)]
    journal.record(
        "output_key_order", observed, f"schema order {declared}", ok=observed == declared
    )
    if observed != declared:
        fail("output_key_order", observed, f"the schema's property order {declared}")


@pytest.mark.case_id("evolution.unicode_escapes")
def test_unicode_escapes_render_like_utf8(client, surface, model, ingress):
    _declared(ingress, "unicode_escapes")
    body = surface.minimal_body(model.public_id, budget_for(model), prompt=UNICODE_TEXT)
    utf8 = json.dumps(body, ensure_ascii=False).encode("utf-8")
    escaped = json.dumps(body, ensure_ascii=True).replace("/", "\\/").encode("ascii")
    if b"\\ud83d\\ude80" not in escaped:
        raise AssertionError("llmdv defect: the escaped spelling must carry a surrogate pair")
    plain = input_tokens(client, surface, model, ingress, raw_body=utf8)
    spelled = input_tokens(client, surface, model, ingress, raw_body=escaped)
    journal.record(
        "input_tokens_utf8_vs_escaped",
        [plain, spelled],
        "equal: both spellings decode to one request",
        ok=plain == spelled,
    )
    if plain != spelled:
        fail("unicode_escapes", [plain, spelled], "the same input token count for both spellings")
