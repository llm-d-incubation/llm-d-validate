"""Every prompt form, echo, choices and streaming of the prompt API.

Harnesses send batches of prompts and pre-tokenized prompts to `/v1/completions`, ask for the
prompt back with `echo`, and compare streamed text with the JSON result. Each form has its own
observable effect: one choice per prompt, a prompt count equal to the ids sent, the prompt at
the head of the text, the same text either way.
"""

from __future__ import annotations

import pytest

from ...assertions import assert_usage_present_and_consistent, fail
from ...evidence import journal
from ..conftest import budget_for, check_choice, check_completion, throttle_aware
from ..openai_contract import (
    assemble_stream,
    check_generation,
    require_contract,
    stream_frames,
)

pytestmark = pytest.mark.surface("completions")

IDS = [1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007]
FORMS = {
    "strings": (["Say ok.", "Say no."], 2, None),
    "token_ids": (IDS, 1, len(IDS)),
    "token_id_lists": ([IDS[:4], IDS[4:]], 2, len(IDS)),
}


def _choices(response: dict, count: int) -> list[dict]:
    choices = response.get("choices")
    indices = sorted(
        c.get("index") for c in choices or [] if isinstance(c, dict) and type(c.get("index")) is int
    )
    if not (isinstance(choices, list) and indices == list(range(count))):
        fail("choice_indices", indices, f"indexes 0..{count - 1}, one per prompt and sample")
    journal.record("choice_indices", indices, f"indexes 0..{count - 1}", ok=True)
    return choices


@pytest.mark.case_id("openai_completions.prompt_forms")
@pytest.mark.parametrize("form", list(FORMS))
def test_completions_prompt_forms(client, surface, model, ingress, form):
    require_contract(ingress, surface)
    prompt, count, prompt_tokens = FORMS[form]
    body = surface.minimal_body(model.public_id, 8, prompt)
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    for choice in _choices(completion.raw, count):
        if not isinstance(choice.get("text"), str):
            fail("choice_text", choice.get("text"), "a string")
    if prompt_tokens is not None:
        observed = completion.usage.prompt_tokens
        if observed != prompt_tokens:
            fail("prompt_tokens", observed, f"{prompt_tokens}, the number of ids sent")
        journal.record("prompt_tokens", observed, f"{prompt_tokens} ids sent", ok=True)


@pytest.mark.case_id("openai_completions.echo")
def test_completions_echo(client, surface, model, ingress):
    require_contract(ingress, surface)
    prompt = "The capital of France is"
    body = surface.minimal_body(model.public_id, 8, prompt, echo=True)
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    text = completion.raw["choices"][0].get("text") or ""
    if not text.startswith(prompt):
        fail("echoed_prompt", text, f"text starting with {prompt!r}")
    journal.record("echoed_prompt", text, f"text starting with {prompt!r}", ok=True)


@pytest.mark.case_id("openai_completions.multiple_choices")
def test_completions_multiple_choices(client, surface, model, ingress):
    require_contract(ingress, surface)
    if "n" not in ingress.capabilities.chat_options:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare n")
    body = surface.minimal_body(
        model.public_id, budget_for(model), surface.reply_prompt, n=2, temperature=1
    )
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    for choice in _choices(completion.raw, 2):
        check_choice(
            surface.parse_completion({**completion.raw, "choices": [choice]}), surface, model
        )


def _json_side(completion) -> list:
    return [
        completion.raw["choices"][0].get("text"),
        completion.usage.prompt_tokens,
        completion.usage.completion_tokens,
    ]


@pytest.mark.case_id("openai_completions.streaming_matches_json")
def test_completions_streaming_matches_json(client, surface, model, ingress):
    """At temperature 0 a stream carries the same text and counts as the JSON answer.

    A stream can only be held to the JSON answer where greedy decoding repeats, so two JSON
    requests go first. If they differ, the fleet is not deterministic: that fails as
    `greedy_repeat`, the promise `chat_repeats: greedy` makes, and the stream is not judged.
    """
    require_contract(ingress, surface)
    if "greedy" not in ingress.capabilities.chat_repeats:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare chat_repeats greedy")
    prompt = "The three primary colors are"
    body = surface.minimal_body(model.public_id, 24, prompt)
    repeats = [
        _json_side(
            check_generation(client.post(surface.path, json_body=body), surface, model, ingress)
        )
        for _ in range(2)
    ]
    expected = "the same text and token counts for two temperature-0 requests"
    if repeats[0] != repeats[1]:
        fail("greedy_repeat", repeats, f"{expected}; the fleet is not deterministic")
    journal.record("greedy_repeat", repeats[0], expected, ok=True)
    body = surface.stream_body(model.public_id, 24, include_usage=True, prompt=prompt)
    streamed = assemble_stream(surface, stream_frames(client, surface, body, ingress))
    check_completion(streamed, surface, model)
    pair = {
        "json": repeats[0],
        "sse": [
            "".join(c.get("text") or "" for f in streamed.raw["chunks"] for c in f["choices"]),
            streamed.usage.prompt_tokens,
            streamed.usage.completion_tokens,
        ],
    }
    if pair["json"] != pair["sse"]:
        fail("stream_matches_json", pair, "the same text and token counts")
    journal.record("stream_matches_json", pair, "the same text and token counts", ok=True)


@pytest.mark.case_id("openai_completions.basic_parses_with_openai_sdk")
def test_completions_parse_with_pinned_sdk(openai_client, surface, model, ingress):
    require_contract(ingress, surface)
    with throttle_aware(ingress):
        result = openai_client.completions.create(
            model=model.public_id,
            prompt=surface.reply_prompt,
            max_tokens=budget_for(model),
            temperature=0,
        )
    completion = surface.parse_completion(result.to_dict())
    check_completion(completion, surface, model)
    assert_usage_present_and_consistent(completion)
