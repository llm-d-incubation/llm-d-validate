"""Instructions and input items must reach the model: each shape carries a code word the
answer can only contain if the layer passed that part of the request through.

A replayed assistant turn carries a word the model chose itself: the history asks it to choose
one, the assistant item states the choice, and the last turn asks which word it chose. A model
that reads the item answers with the word; one that never saw it cannot. Asking it to repeat a
word it was simply told it said invites a correct model to decline an unsupported claim."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ...assertions import assert_answer_only, assert_has_answer, fail
from ...evidence import journal
from ..conftest import budget_for, check_completion, check_json_response

pytestmark = pytest.mark.surface("responses")

CODE_WORD = "basalt"
FACT = f"The code word is {CODE_WORD}."
QUESTION = "What is the code word? Reply with the code word only."
# The replayed conversation for the assistant shapes: the model's own choice, then a recall.
CHOOSE = "Choose a code word, any single English word, and tell me which word you chose."
CHOSEN = f"I chose {CODE_WORD}."
RECALL = "Which code word did you choose? Reply with the code word only."


def _assistant(text: str, **extra: Any) -> dict[str, Any]:
    """A prior assistant turn, replayed the way a stateless client sends it back."""
    return {
        "type": "message",
        "id": f"msg_llmdv{uuid.uuid4().hex[:16]}",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
        **extra,
    }


def _reasoning(*, summary_only: bool = False) -> dict[str, Any]:
    thought = "The user asks me to choose a code word. I will choose a single English word."
    item: dict[str, Any] = {"type": "reasoning", "id": f"rs_llmdv{uuid.uuid4().hex[:16]}"}
    if summary_only:
        # The shape a hosted API returns: a summary and no raw reasoning text.
        return {**item, "summary": [{"type": "summary_text", "text": thought}]}
    return {**item, "summary": [], "content": [{"type": "reasoning_text", "text": thought}]}


def input_items(shape: str) -> list[dict[str, Any]]:
    question = {"role": "user", "content": QUESTION}
    if shape in ("system", "developer"):
        return [{"type": "message", "role": shape, "content": FACT}, question]
    if shape == "developer_with_instructions":
        return [{"type": "message", "role": "developer", "content": FACT}, question]
    if shape == "input_text_parts":
        parts = [{"type": "input_text", "text": FACT}, {"type": "input_text", "text": QUESTION}]
        return [{"type": "message", "role": "user", "content": parts}]
    ask = {"role": "user", "content": CHOOSE}
    recall = {"role": "user", "content": RECALL}
    if shape == "assistant_replay":
        return [ask, _assistant(CHOSEN), recall]
    if shape == "reasoning_replay":
        return [ask, _reasoning(), _assistant(CHOSEN), recall]
    if shape == "reasoning_summary_replay":
        return [ask, _reasoning(summary_only=True), _assistant(CHOSEN), recall]
    if shape == "assistant_phase":
        return [ask, _assistant(CHOSEN, phase="final_answer"), recall]
    if shape == "assistant_text":
        return [ask, {"role": "assistant", "content": CHOSEN}, recall]
    raise ValueError(f"unknown input shape {shape!r}")


def _answer(client, surface, model, ingress, **extra: Any) -> dict[str, Any]:
    budget = budget_for(model, floor=128)
    body = surface.minimal_body(model.public_id, max_tokens=budget, **extra)
    response = check_json_response(client.post(surface.path, json_body=body), ingress)
    completion = surface.parse_completion(response)
    check_completion(completion, surface, model)
    assert_has_answer(completion, budget)
    assert_answer_only(completion, model, CODE_WORD)
    return response


@pytest.mark.case_id("openai_responses.instructions")
def test_instructions_reach_the_model(client, surface, model, ingress):
    """`instructions` carries the code word; an echoed value must be the one sent."""
    response = _answer(client, surface, model, ingress, prompt=QUESTION, instructions=FACT)
    echoed = response.get("instructions")
    ok = echoed in (None, FACT)
    journal.record("instructions_echo", echoed, f"{FACT!r}, or absent", ok=ok)
    if not ok:
        fail("instructions_echo", echoed, FACT)


# Sent beside the items of a shape: an agent puts its system prompt in `instructions` and its
# policy in a developer item, and both must reach the model.
SHAPE_FIELDS = {"developer_with_instructions": {"instructions": "Keep every answer short."}}


@pytest.mark.case_id("openai_responses.input_items")
@pytest.mark.parametrize(
    "shape",
    [
        "system",
        "developer",
        "developer_with_instructions",
        "input_text_parts",
        "assistant_replay",
        "reasoning_replay",
        "reasoning_summary_replay",
        "assistant_phase",
        "assistant_text",
    ],
)
def test_input_items_reach_the_model(client, surface, model, ingress, shape):
    """Each item shape a stateless client sends is read as part of the conversation."""
    items = input_items(shape)
    journal.record("input_item_types", [i.get("type", "message") for i in items], shape)
    _answer(client, surface, model, ingress, prompt=items, **SHAPE_FIELDS.get(shape, {}))


COLOR = "Name one color. Reply with the color only."
REPEAT = "Repeat your previous answer exactly, with nothing else."


@pytest.mark.case_id("openai_responses.output_replay")
def test_output_items_replay_as_returned(client, surface, model, ingress):
    """A stateless client replays the fleet's own output items, nulls and all, as history.

    The second answer can only repeat the first if the replayed items were read.
    """
    budget = budget_for(model, floor=128)
    body = surface.minimal_body(model.public_id, budget, prompt=COLOR)
    first = check_json_response(client.post(surface.path, json_body=body), ingress)
    said = surface.parse_completion(first)
    check_completion(said, surface, model)
    answer = said.text.strip()
    journal.record("first_answer", answer, "a non-empty answer to replay", ok=bool(answer))
    if not answer:
        fail("first_answer", answer, "a non-empty answer to replay")
    history = [
        {"role": "user", "content": COLOR},
        *first["output"],
        {"role": "user", "content": REPEAT},
    ]
    journal.record("replayed_items", [i.get("type") for i in first["output"]], "as returned")
    _answer_is(client, surface, model, ingress, answer, prompt=history)


def _answer_is(client, surface, model, ingress, expected: str, **extra: Any) -> None:
    budget = budget_for(model, floor=128)
    body = surface.minimal_body(model.public_id, max_tokens=budget, **extra)
    response = check_json_response(client.post(surface.path, json_body=body), ingress)
    completion = surface.parse_completion(response)
    check_completion(completion, surface, model)
    assert_has_answer(completion, budget)
    assert_answer_only(completion, model, expected)


@pytest.mark.case_id("openai_responses.encrypted_replay")
def test_encrypted_reasoning_replays(client, surface, model, ingress):
    """A stateless client asks for encrypted reasoning, then sends those items back with the
    next turn. Where the include is declared accepted, the replay is read and never refused."""
    if ingress.capabilities.responses_features.get("encrypted_reasoning") != "accepted":
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not accept encrypted reasoning")
    sealed = {"include": ["reasoning.encrypted_content"], "store": False}
    budget = budget_for(model, floor=128)
    body = surface.minimal_body(model.public_id, budget, prompt=COLOR, **sealed)
    first = check_json_response(client.post(surface.path, json_body=body), ingress)
    said = surface.parse_completion(first)
    check_completion(said, surface, model)
    answer = said.text.strip()
    journal.record("first_answer", answer, "a non-empty answer to replay", ok=bool(answer))
    if not answer:
        fail("first_answer", answer, "a non-empty answer to replay")
    carried = [
        i for i in first["output"] if i.get("type") == "reasoning" and i.get("encrypted_content")
    ]
    journal.record("encrypted_items", len(carried), "reasoning items replayed with their content")
    history = [
        {"role": "user", "content": COLOR},
        *first["output"],
        {"role": "user", "content": REPEAT},
    ]
    _answer_is(client, surface, model, ingress, answer, prompt=history, **sealed)


LOOKUP = {
    "type": "function",
    "name": "get_code_word",
    "description": "Returns the current code word.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


@pytest.mark.case_id("openai_responses.function_output")
@pytest.mark.needs("tools")
@pytest.mark.parametrize("form", ["text", "parts"])
def test_function_output_reaches_the_model(client, surface, model, ingress, form):
    """A tool result reaches the model as a string or as a list of input_text parts."""
    call_id = f"call_llmdv{uuid.uuid4().hex[:16]}"
    output: Any = FACT if form == "text" else [{"type": "input_text", "text": FACT}]
    items = [
        {"role": "user", "content": "Look up the code word."},
        {"type": "function_call", "call_id": call_id, "name": LOOKUP["name"], "arguments": "{}"},
        {"type": "function_call_output", "call_id": call_id, "output": output},
        {"role": "user", "content": f"Do not call any tool. {QUESTION}"},
    ]
    journal.record("function_output_form", form, "the tool result shape")
    _answer(client, surface, model, ingress, prompt=items, tools=[LOOKUP], tool_choice="auto")
