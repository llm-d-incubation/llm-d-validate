"""Sampling repeats: a seed reproduces, greedy decoding repeats, every request is fresh.

Evaluation clients set a seed to reproduce a run, regression harnesses diff greedy outputs,
and every client expects a new generation per request. A response cache in a gateway replays
the first answer, which also makes every repeat-request measurement read that cache.
Each case runs where `capabilities.chat_repeats` names it and sends the n requests its
`samples` rule declares, two by default.

OpenAI documents `seed` as best effort, and vLLM does not promise identical repeats across
batch compositions, so the seed and greedy cases measure how many repeats agree and pass either
way, unless the profile declares `deterministic_repeats`: then their rule is the verdict. Every
generation being fresh is a promise everywhere, so that case always judges.

Where greedy decoding repeats, a stream also carries the same answer as JSON: a stream path
that splits reasoning from content differently from the JSON path shows up as a difference.
"""

from __future__ import annotations

import uuid

import pytest

from ...assertions import fail
from ...assertions.samples import draw, judge, sample
from ...evidence import journal
from ..conftest import budget_for, check_basic_completion, check_completion, last_seq
from ..openai_contract import assemble_stream, stream_frames

pytestmark = pytest.mark.surface("chat_completions")


def _draw(client, surface, model, ingress, samples, **sampling):
    """The rule's n completions of one prompt, each with its sample of text and reasoning."""
    prompt = f"Reference {uuid.uuid4().hex}. Write a random six-word sentence."
    body = surface.minimal_body(model.public_id, budget_for(model), prompt, **sampling)
    drawn = []

    def take(_group):
        exchange = client.post(surface.path, json_body=body)
        completion = check_basic_completion(exchange, surface, model, ingress)
        drawn.append(completion)
        texts = [completion.text, completion.reasoning_text]
        return sample(texts, last_seq(client), preview=completion.text)

    return drawn, draw(samples, take)


def _require(ingress, name: str) -> None:
    if name not in ingress.capabilities.chat_repeats:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare chat_repeats {name}")


def _repeats(ingress, samples, drawn, why: str) -> None:
    """Agreement among the repeats, judged only where the profile promises determinism."""
    judge("repeat_text", samples, drawn, why, decides=ingress.capabilities.deterministic_repeats)


@pytest.mark.samples("count", n=2, min=2)
@pytest.mark.case_id("openai_chat.seed_repeat")
def test_chat_seed_repeat(client, surface, model, ingress, samples):
    _require(ingress, "seed")
    _, drawn = _draw(client, surface, model, ingress, samples, temperature=1, seed=1234)
    _repeats(ingress, samples, drawn, "the same text for the same seed at temperature 1")


@pytest.mark.samples("count", n=2, min=2)
@pytest.mark.case_id("openai_chat.greedy_repeat")
def test_chat_greedy_repeat(client, surface, model, ingress, samples):
    _require(ingress, "greedy")
    _, drawn = _draw(client, surface, model, ingress, samples, temperature=0)
    _repeats(ingress, samples, drawn, "the same text for greedy decoding")


@pytest.mark.samples("count", n=2, max=1)
@pytest.mark.case_id("openai_chat.fresh_generation")
def test_chat_fresh_generation(client, surface, model, ingress, samples):
    """Identical sampled requests get distinct generations, each with its own id."""
    _require(ingress, "fresh")
    completions, drawn = _draw(client, surface, model, ingress, samples, temperature=1)
    judge("fresh_generation", samples, drawn, "a different text for every request")
    ids = [c.raw.get("id") for c in completions]
    if len(set(ids)) != len(ids):
        fail("fresh_ids", ids, "a different id for every request")
    journal.record("fresh_ids", ids, "a different id for every request", ok=True)


@pytest.mark.case_id("openai_chat.streaming_matches_json")
def test_chat_streaming_matches_json(client, surface, model, ingress):
    """At temperature 0 the stream carries the same answer, reasoning and counts as JSON."""
    _require(ingress, "greedy")
    budget = budget_for(model)
    body = surface.minimal_body(model.public_id, budget, temperature=0)
    whole = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    body = surface.stream_body(model.public_id, budget, include_usage=True, temperature=0)
    streamed = assemble_stream(surface, stream_frames(client, surface, body, ingress))
    check_completion(streamed, surface, model)
    pair = {
        side: [c.text, c.reasoning_text, c.usage.prompt_tokens, c.usage.completion_tokens]
        for side, c in (("json", whole), ("sse", streamed))
    }
    if pair["json"] != pair["sse"]:
        fail("stream_matches_json", pair, "the same answer, reasoning and token counts")
    journal.record("stream_matches_json", pair["json"][0], "the same as JSON", ok=True)
