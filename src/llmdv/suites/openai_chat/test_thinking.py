"""Reasoning controls a client sends on Chat, and reasoning it sends back.

Clients turn thinking off with `chat_template_kwargs.enable_thinking: false`, the convention of
several open model families, and agent clients replay whole assistant messages, reasoning
field included, on the next turn. The first must leave the answer channel holding the answer
alone; the second must be accepted and answered. A token limit that ends the reply mid-thought
must leave the cut reasoning in the reasoning channel, where clients that read only `content`
cannot mistake it for an answer. A client lowers `reasoning_effort` to save time and tokens,
so a lower declared effort must not reason more than a higher one. Temperature 0 does not
make a reasoning length repeat on every engine, so each effort is sampled several times and
the medians are compared. A client that sets `include_reasoning: false` saves the reasoning
bytes and still needs the answer, alone, in `content`.
"""

from __future__ import annotations

from statistics import median

import pytest

from ...assertions import assert_answer_only, fail
from ...evidence import journal
from ...profile.schema import CHAT_REASONING_EFFORTS
from ...surfaces.base import REASONING_ANSWER, REASONING_PROMPT
from ..conftest import budget_for, check_basic_completion, check_completion
from ..openai_contract import (
    assemble_stream,
    check_generation,
    require_contract,
    require_option,
    stream_frames,
)

pytestmark = pytest.mark.surface("chat_completions")

# A prompt a reasoning model thinks about for far longer than CUT_TOKENS.
PROOF_PROMPT = "Prove that there are infinitely many primes, carefully."
CUT_TOKENS = 40
EFFORT_PROMPT = (
    'How many times does the letter r appear in "strawberry raspberry"? Reply with the number only.'
)
# Efforts that render the same template level differ only by batch noise at temperature 0;
# this slack absorbs that noise and still catches a lower effort reasoning several times more.
EFFORT_SLACK = 1.25
# Requests per effort, each planned up front and none a retry. One sample per effort flaps
# where temperature 0 is not deterministic; the median of three does not let one outlier
# decide. Six efforts besides `none` are declarable, so the case sends at most 18 requests.
EFFORT_SAMPLES = 3


@pytest.mark.needs("reasoning")
@pytest.mark.case_id("openai_chat.thinking_off")
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_chat_thinking_off(client, surface, model, ingress, stream):
    """enable_thinking false returns the answer alone, with no reasoning in content."""
    require_option(ingress, "enable_thinking")
    kwargs = {"chat_template_kwargs": {"enable_thinking": False}}
    _check_answer_alone(client, surface, model, ingress, stream, "thinking_off", **kwargs)


@pytest.mark.needs("reasoning")
@pytest.mark.case_id("openai_chat.reasoning_hidden")
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_chat_reasoning_hidden(client, surface, model, ingress, stream):
    """include_reasoning false returns the answer alone, with no reasoning in any channel."""
    require_option(ingress, "include_reasoning")
    _check_answer_alone(
        client, surface, model, ingress, stream, "reasoning_hidden", include_reasoning=False
    )


def _check_answer_alone(client, surface, model, ingress, stream, name, **kwargs):
    budget = budget_for(model, floor=256)
    if stream:
        body = surface.stream_body(
            model.public_id, budget, include_usage=True, prompt=REASONING_PROMPT, **kwargs
        )
        completion = assemble_stream(surface, stream_frames(client, surface, body, ingress))
        check_completion(completion, surface, model)
    else:
        body = surface.minimal_body(model.public_id, budget, REASONING_PROMPT, **kwargs)
        completion = check_basic_completion(
            client.post(surface.path, json_body=body), surface, model, ingress
        )
    if completion.reasoning_text.strip():
        fail(name, completion.reasoning_text[:200], f"no reasoning with {name}")
    journal.record(name, completion.text[:80], "no reasoning", ok=True)
    assert_answer_only(completion, model, REASONING_ANSWER)


@pytest.mark.needs("reasoning")
@pytest.mark.case_id("openai_chat.reasoning_history")
def test_chat_reasoning_history(client, surface, model, ingress):
    """A follow-up that replays the assistant turn with its reasoning field is answered."""
    require_contract(ingress, surface)
    budget = budget_for(model, floor=256)
    body = surface.minimal_body(model.public_id, budget, REASONING_PROMPT)
    first = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    field = model.reasoning.field_name or "reasoning"
    body["messages"] += [
        {"role": "assistant", "content": first.text, field: first.reasoning_text},
        {"role": "user", "content": "Add 9 to that. Reply with the number only."},
    ]
    journal.record("replayed_reasoning_field", field)
    second = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    assert_answer_only(second, model, "400")


@pytest.mark.needs("reasoning")
@pytest.mark.case_id("openai_chat.reasoning_cut_by_length")
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_chat_reasoning_cut_by_length(client, surface, model, ingress, stream):
    """A limit reached mid-thought ends with finish length and no answer text."""
    require_contract(ingress, surface)
    if not model.reasoning.default_on:
        pytest.skip("[undeclared] the model declares no reasoning by default")
    journal.record("max_tokens", CUT_TOKENS)
    if stream:
        body = surface.stream_body(
            model.public_id, CUT_TOKENS, include_usage=True, prompt=PROOF_PROMPT, temperature=0
        )
        completion = assemble_stream(surface, stream_frames(client, surface, body, ingress))
    else:
        body = surface.minimal_body(model.public_id, CUT_TOKENS, PROOF_PROMPT, temperature=0)
        exchange = client.post(surface.path, json_body=body)
        completion = check_generation(exchange, surface, model, ingress)
    finish = completion.finish_reason
    journal.record("finish_reason", finish, "length", ok=finish == "length")
    if finish != "length":
        fail("finish_reason", finish, f"length after {CUT_TOKENS} tokens of reasoning")
    observed = {"content": completion.text[:200], "reasoning": completion.reasoning_text[:200]}
    if completion.text.strip():
        fail("cut_reasoning_channel", observed, "no answer text when the limit cuts reasoning")
    if not completion.reasoning_text.strip():
        fail("cut_reasoning_channel", observed, "the cut reasoning in the reasoning channel")
    journal.record("cut_reasoning_channel", observed, "reasoning only", ok=True)


@pytest.mark.needs("reasoning")
@pytest.mark.case_id("openai_chat.reasoning_effort_order")
def test_chat_reasoning_effort_order(client, surface, model, ingress):
    """Over EFFORT_SAMPLES requests per effort, the lowest declared effort's median reasons
    less than the highest's, and no effort's median is far above a higher one's."""
    require_contract(ingress, surface)
    declared = [
        effort
        for effort in CHAT_REASONING_EFFORTS
        if effort in ingress.capabilities.chat_reasoning_efforts and effort != "none"
    ]
    if len(declared) < 2:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares fewer than two Chat efforts")
    body = {
        effort: surface.minimal_body(
            model.public_id,
            budget_for(model, floor=512),
            EFFORT_PROMPT,
            reasoning_effort=effort,
            temperature=0,
        )
        for effort in declared
    }
    samples: dict[str, list[int]] = {effort: [] for effort in declared}
    # Round-robin over the efforts, so drift in the fleet's load over the run spreads evenly.
    for _ in range(EFFORT_SAMPLES):
        for effort in declared:
            completion = check_basic_completion(
                client.post(surface.path, json_body=body[effort]), surface, model, ingress
            )
            samples[effort].append(completion.usage.completion_tokens)
            journal.record(f"completion_tokens[{effort}]", samples[effort][-1])
    tokens = {effort: median(counts) for effort, counts in samples.items()}
    journal.record("completion_tokens_by_effort", samples, f"{EFFORT_SAMPLES} samples per effort")
    journal.record("median_completion_tokens_by_effort", tokens)
    if not tokens[declared[0]] < tokens[declared[-1]]:
        fail("effort_order", tokens, f"median {declared[0]} below median {declared[-1]}")
    inverted = [
        (low, high)
        for i, low in enumerate(declared)
        for high in declared[i + 1 :]
        if tokens[low] > tokens[high] * EFFORT_SLACK
    ]
    if inverted:
        fail(
            "effort_order",
            {"medians": tokens, "inverted": inverted},
            "no lower effort's median far above a higher one's",
        )
    journal.record(
        "effort_order", tokens, "lower efforts' medians no more than higher ones'", ok=True
    )
