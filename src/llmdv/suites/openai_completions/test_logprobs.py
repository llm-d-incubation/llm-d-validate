"""Integer logprobs on the prompt API: aligned arrays for every completion token.

Scoring harnesses ask for `echo` with `logprobs` and a one-token completion, then read the
logprob of every prompt token after the first. The arrays must cover the whole prompt.
"""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...assertions.token_logprobs import assert_text_logprobs
from ...evidence import journal
from ..openai_contract import check_generation, require_contract

pytestmark = pytest.mark.surface("completions")

WIDTH = 2
SCORED = "The capital of France is Paris"


@pytest.mark.case_id("openai_completions.logprobs")
def test_completions_logprobs(client, surface, model, ingress):
    require_contract(ingress, surface)
    body = surface.minimal_body(model.public_id, 12, surface.reply_prompt, logprobs=WIDTH)
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    logprobs = completion.raw["choices"][0].get("logprobs")
    assert_text_logprobs(logprobs, completion.usage.completion_tokens, WIDTH)


@pytest.mark.case_id("openai_completions.echo_logprobs")
def test_completions_scored_prompt(client, surface, model, ingress):
    """Echo with logprobs scores the prompt: one entry per prompt and completion token."""
    require_contract(ingress, surface)
    body = surface.minimal_body(model.public_id, 1, SCORED, echo=True, logprobs=1, temperature=0)
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    choice = completion.raw["choices"][0]
    text = choice.get("text") or ""
    if not text.startswith(SCORED):
        fail("echoed_prompt", text, f"text starting with {SCORED!r}")
    usage = completion.usage
    count = usage.prompt_tokens + usage.completion_tokens
    journal.record("scored_tokens", count, "prompt plus completion tokens")
    assert_text_logprobs(choice.get("logprobs"), count, 1, first_unscored=True)
