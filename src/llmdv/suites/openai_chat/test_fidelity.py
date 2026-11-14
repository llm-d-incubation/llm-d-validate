"""The body the client sent is the body the model sees: numbers and key order intact.

A hop that decodes a request into generic maps and encodes it again changes it silently:
integers above 2^53 become floats, and object keys come out sorted. A tool schema bound or a
64-bit record id then reaches the model changed, and a structured output whose schema puts
reasoning before the answer is generated answer first.
"""

from __future__ import annotations

import json

import pytest

from ...assertions import fail
from ...assertions.structured_output import assert_structured_reasoning
from ...evidence import journal
from ..conftest import budget_for, check_basic_completion
from ..openai_contract import check_generation, require_contract

pytestmark = pytest.mark.surface("chat_completions")

# Above 2^64; a float64 re-encode writes it as 1.2345678901234568e+22.
BIG = 12345678901234567890123
BIG_AS_FLOAT = 1.2345678901234568e22
QUOTE = (
    "Do not call any tool. Quote the exact maximum value of the count parameter in the "
    "set_count tool schema. Reply with that value only."
)


def _tool(bound) -> dict:
    count = {"type": "integer", "maximum": bound}
    return {
        "type": "function",
        "function": {
            "name": "set_count",
            "description": "Set the counter.",
            "parameters": {"type": "object", "properties": {"count": count}, "required": ["count"]},
        },
    }


@pytest.mark.needs("tools")
@pytest.mark.case_id("openai_chat.number_fidelity")
def test_chat_number_fidelity(client, surface, model, ingress):
    """The integer and its float spelling render differently, unless a hop converted it.

    The oracle does not depend on the model: the engine renders the schema into the prompt, so
    the two spellings give different prompt counts. Equal counts mean the integer arrived as
    the float. Where the model declares automatic tool choice the tool is offered under `auto`,
    because `none` can leave it out of the prompt (vLLM `--exclude-tools-when-tool-choice-none`);
    the prompt says not to call it, and a call still carries the prompt count.
    """
    require_contract(ingress, surface)
    choice = "auto" if model.tools.auto_choice else "none"
    journal.record("tool_choice", choice)
    counts = {}
    for name, bound in (("integer", BIG), ("float", BIG_AS_FLOAT)):
        body = surface.minimal_body(
            model.public_id, budget_for(model), QUOTE, tools=[_tool(bound)], tool_choice=choice
        )
        completion = check_generation(
            client.post(surface.path, json_body=body), surface, model, ingress
        )
        counts[name] = completion.usage.prompt_tokens
        journal.record(f"{name}_quote", completion.text[:120])
    if counts["integer"] == counts["float"]:
        fail("number_fidelity", counts, "different prompt counts for the two spellings")
    journal.record("number_fidelity", counts, "different prompt counts", ok=True)


@pytest.mark.needs("structured_output")
@pytest.mark.case_id("openai_chat.structured_key_order")
def test_chat_structured_key_order(client, surface, model, ingress):
    """Structured output keys follow the schema's property order."""
    require_contract(ingress, surface)
    schema = {
        "type": "object",
        "properties": {"reasoning": {"type": "string"}, "answer": {"type": "integer"}},
        "required": ["reasoning", "answer"],
        "additionalProperties": False,
    }
    body = surface.structured_output_body(
        model.public_id,
        schema,
        budget_for(model, floor=256),
        "What is 17 times 23? Explain briefly, then answer.",
    )
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    try:
        keys = [key for key, _ in json.loads(completion.text, object_pairs_hook=list)]
    except (TypeError, ValueError):
        keys = None
    if keys != ["reasoning", "answer"]:
        fail("structured_key_order", keys or completion.text[:200], "['reasoning', 'answer']")
    journal.record("structured_key_order", keys, "the schema's property order", ok=True)
    # The schema's "reasoning" string is part of the answer, not the model's reasoning channel.
    assert_structured_reasoning(completion, model)
