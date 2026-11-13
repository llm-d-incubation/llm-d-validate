"""Sampling repeats: a seed reproduces, greedy decoding repeats, every request is fresh.

Evaluation clients set a seed to reproduce a run, regression harnesses diff greedy outputs,
and every client expects a new generation per request. A response cache in a gateway replays
the first answer, which also makes every repeat-request measurement read that cache.
Each case sends two requests and runs where `capabilities.chat_repeats` names it. Where greedy
decoding repeats, a stream also carries the same answer as JSON: a stream path that splits
reasoning from content differently from the JSON path shows up as a difference.
"""

from __future__ import annotations

import uuid

import pytest

from ...assertions import fail
from ...evidence import journal
from ..conftest import budget_for, check_basic_completion, check_completion
from ..openai_contract import assemble_stream, stream_frames

pytestmark = pytest.mark.surface("chat_completions")


def _pair(client, surface, model, ingress, **sampling):
    prompt = f"Reference {uuid.uuid4().hex}. Write a random six-word sentence."
    body = surface.minimal_body(model.public_id, budget_for(model), prompt, **sampling)
    return [
        check_basic_completion(client.post(surface.path, json_body=body), surface, model, ingress)
        for _ in range(2)
    ]


def _require(ingress, name: str) -> None:
    if name not in ingress.capabilities.chat_repeats:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare chat_repeats {name}")


def _same(first, second, why: str) -> None:
    texts = [(c.text, c.reasoning_text) for c in (first, second)]
    if texts[0] != texts[1]:
        fail("repeat_text", texts, why)
    journal.record("repeat_text", texts[0][0], why, ok=True)


@pytest.mark.case_id("openai_chat.seed_repeat")
def test_chat_seed_repeat(client, surface, model, ingress):
    _require(ingress, "seed")
    first, second = _pair(client, surface, model, ingress, temperature=1, seed=1234)
    _same(first, second, "the same text for the same seed at temperature 1")


@pytest.mark.case_id("openai_chat.greedy_repeat")
def test_chat_greedy_repeat(client, surface, model, ingress):
    _require(ingress, "greedy")
    first, second = _pair(client, surface, model, ingress, temperature=0)
    _same(first, second, "the same text for greedy decoding")


@pytest.mark.case_id("openai_chat.fresh_generation")
def test_chat_fresh_generation(client, surface, model, ingress):
    """Two identical sampled requests get two distinct generations."""
    _require(ingress, "fresh")
    first, second = _pair(client, surface, model, ingress, temperature=1)
    observed = {
        "ids": [c.raw.get("id") for c in (first, second)],
        "texts": [c.text + c.reasoning_text for c in (first, second)],
    }
    if observed["ids"][0] == observed["ids"][1] or observed["texts"][0] == observed["texts"][1]:
        fail("fresh_generation", observed, "different ids and different texts")
    journal.record("fresh_generation", observed["ids"], "different ids and texts", ok=True)


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
