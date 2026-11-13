"""Engine options on the prompt API, each judged by its observable effect.

With `return_token_ids`, vLLM puts the prompt and output token ids on each choice; trainers
and evaluators read them to score exactly the tokens the engine saw and sampled.
`truncate_prompt_tokens` caps the prompt the engine reads, so a client can send an overlong
prompt and still get an answer. A layer that accepts either option and drops its effect
answers 200, so each case asserts the effect. Each runs where the profile enables the
contract and `capabilities.vllm_options` names the option.
"""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...evidence import journal
from ..openai_contract import check_generation, require_contract, require_option

pytestmark = pytest.mark.surface("completions")

LONG_PROMPT = "word " * 64
TRUNCATE = 8


@pytest.mark.case_id("openai_completions.token_ids")
def test_completions_token_ids(client, surface, model, ingress):
    """The choice carries one prompt id per prompt token and one id per output token."""
    require_contract(ingress, surface)
    require_option(ingress, "return_token_ids")
    body = surface.minimal_body(model.public_id, 16, return_token_ids=True)
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    choice = completion.raw["choices"][0]
    for name, count in (
        ("prompt_token_ids", completion.usage.prompt_tokens),
        ("token_ids", completion.usage.completion_tokens),
    ):
        values = choice.get(name)
        expected = f"{count} non-negative integer ids"
        ok = (
            isinstance(values, list)
            and len(values) == count
            and all(type(value) is int and value >= 0 for value in values)
        )
        journal.record(name, values, expected, ok=ok)
        if not ok:
            fail(name, values, expected)


@pytest.mark.case_id("openai_completions.truncated_prompt")
def test_completions_truncated_prompt(client, surface, model, ingress):
    """truncate_prompt_tokens bounds the prompt tokens the engine reads."""
    require_contract(ingress, surface)
    require_option(ingress, "truncate_prompt_tokens")
    counts = []
    for extra in ({}, {"truncate_prompt_tokens": TRUNCATE}):
        body = surface.minimal_body(model.public_id, 8, LONG_PROMPT, **extra)
        completion = check_generation(
            client.post(surface.path, json_body=body), surface, model, ingress
        )
        counts.append(completion.usage.prompt_tokens)
    full, truncated = counts
    journal.record("truncation_control", full, f"more than {TRUNCATE}", ok=full > TRUNCATE)
    if not full > TRUNCATE:
        fail("truncation_control", full, f"more than {TRUNCATE}")
    expected = f"1..{TRUNCATE}"
    journal.record("truncated_prompt_tokens", truncated, expected, ok=0 < truncated <= TRUNCATE)
    if not 0 < truncated <= TRUNCATE:
        fail("truncated_prompt_tokens", truncated, expected)
