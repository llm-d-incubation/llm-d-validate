"""Engine options on Chat, each judged by its observable effect.

vLLM's Chat server takes options the OpenAI API does not define: assistant prefill with
`continue_final_message`, `echo`, cumulative usage on every chunk, constrained choices through
`structured_outputs` (the successor of `guided_choice`), logprob tokens as ids, prompt
logprobs, a restricted output vocabulary and banned words. `logit_bias` is an OpenAI field
whose effect needs the same kind of check. A layer that accepts one of these and drops its
effect answers 200, so each case asserts the effect. Each runs only where
`capabilities.vllm_options` names the option.
"""

from __future__ import annotations

import re

import pytest

from ...assertions import fail
from ...evidence import journal
from ..conftest import budget_for, check_basic_completion, check_completion
from ..openai_contract import (
    assemble_stream,
    check_generation,
    require_option,
    stream_frames,
)

pytestmark = pytest.mark.surface("chat_completions")

COUNT_PROMPT = "Count from 1 to 10 separated by commas."
# The forced token is not end-of-sequence, so the reply runs to the limit.
FORCED = {"1000": 100}
FORCED_STEPS = 6
BANNED_PROMPT = "Say: red green blue"
BANNED = "green"
# Any ids work: the oracle is that the engine samples only these, whatever they decode to.
ALLOWED_IDS = [1000, 1001, 1002, 1003]
PREFIX = "1, 2, 3, 4,"


@pytest.mark.case_id("openai_chat.continue_final_message")
@pytest.mark.parametrize("variant", ["plain", "echo"])
def test_chat_continue_final_message(client, surface, model, ingress, variant):
    """The reply continues the final assistant message, in the answer channel."""
    require_option(ingress, "continue_final_message")
    if variant == "echo":
        require_option(ingress, "echo")
    body = surface.minimal_body(model.public_id, budget_for(model), COUNT_PROMPT)
    body["messages"].append({"role": "assistant", "content": PREFIX})
    body.update(add_generation_prompt=False, continue_final_message=True)
    if variant == "echo":
        body["echo"] = True
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    text = completion.text
    observed = {"content": text, "reasoning": completion.reasoning_text}
    rest = text[len(PREFIX) :] if variant == "echo" else text
    head_ok = text.startswith(PREFIX) if variant == "echo" else not text.lstrip().startswith("1,")
    wanted = "content continuing the prefix with 5" + (
        ", after the echoed prefix" if variant == "echo" else ""
    )
    if not (head_ok and "5" in rest):
        fail("continued_in_content", observed, wanted)
    journal.record("continued_in_content", observed, wanted, ok=True)


@pytest.mark.case_id("openai_chat.continuous_usage")
def test_chat_continuous_usage(client, surface, model, ingress):
    """Every chunk carries cumulative usage that never decreases and ends at the total."""
    require_option(ingress, "continuous_usage_stats")
    body = surface.stream_body(model.public_id, budget_for(model), include_usage=True)
    body["stream_options"]["continuous_usage_stats"] = True
    frames = stream_frames(client, surface, body, ingress)
    completion = assemble_stream(surface, frames)
    check_completion(completion, surface, model)
    counts = [(frame.get("usage") or {}).get("completion_tokens") for frame in frames]
    prompts = {(frame.get("usage") or {}).get("prompt_tokens") for frame in frames}
    ok = (
        all(type(c) is int for c in counts)
        and counts == sorted(counts)
        and counts[-1] == completion.usage.completion_tokens
        and prompts == {completion.usage.prompt_tokens}
    )
    if not ok:
        fail("continuous_usage", {"completion_tokens": counts, "prompt": prompts}, "cumulative")
    journal.record(
        "continuous_usage", {"chunks": len(counts), "last": counts[-1]}, "cumulative", ok=True
    )


@pytest.mark.case_id("openai_chat.constrained_choice")
@pytest.mark.parametrize("option", ["structured_outputs", "guided_choice"])
def test_chat_constrained_choice(client, surface, model, ingress, option):
    """The answer is exactly one of the allowed choices."""
    require_option(ingress, option)
    choices = ["alpha", "beta"]
    constraint = {"choice": choices} if option == "structured_outputs" else choices
    body = surface.minimal_body(
        model.public_id, budget_for(model), "Pick one word.", **{option: constraint}
    )
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    if completion.text.strip() not in choices:
        fail("constrained_choice", completion.text, f"exactly one of {choices}")
    journal.record("constrained_choice", completion.text, f"one of {choices}", ok=True)


@pytest.mark.case_id("openai_chat.tokens_as_ids")
def test_chat_tokens_as_ids(client, surface, model, ingress):
    """With return_tokens_as_token_ids every logprob token reads token_id:N."""
    require_option(ingress, "return_tokens_as_token_ids")
    body = surface.minimal_body(
        model.public_id, 12, logprobs=True, top_logprobs=1, return_tokens_as_token_ids=True
    )
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    entries = (completion.raw["choices"][0].get("logprobs") or {}).get("content") or []
    tokens = [entry.get("token") for entry in entries if isinstance(entry, dict)]
    if not tokens or not all(
        isinstance(t, str) and re.fullmatch(r"token_id:\d+", t) for t in tokens
    ):
        fail("tokens_as_ids", tokens, "every token as token_id:N")
    journal.record("tokens_as_ids", len(tokens), "every token as token_id:N", ok=True)


@pytest.mark.case_id("openai_chat.prompt_logprobs")
def test_chat_prompt_logprobs(client, surface, model, ingress):
    """One prompt logprob entry per prompt token, the first null."""
    require_option(ingress, "prompt_logprobs")
    body = surface.minimal_body(model.public_id, 8, prompt_logprobs=1)
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    entries = completion.raw.get("prompt_logprobs")
    count = completion.usage.prompt_tokens
    ok = (
        isinstance(entries, list)
        and len(entries) == count
        and entries[0] is None
        and all(isinstance(entry, dict) and entry for entry in entries[1:])
    )
    observed = {"entries": len(entries) if isinstance(entries, list) else entries, "prompt": count}
    if not ok:
        fail("prompt_logprobs", observed, "one entry per prompt token, the first null")
    journal.record("prompt_logprobs", observed, "one entry per prompt token", ok=True)


@pytest.mark.case_id("openai_chat.allowed_tokens")
def test_chat_allowed_tokens(client, surface, model, ingress):
    """With allowed_token_ids every sampled token id is one of the allowed ids."""
    require_option(ingress, "allowed_token_ids")
    require_option(ingress, "return_tokens_as_token_ids")
    body = surface.minimal_body(
        model.public_id,
        8,
        logprobs=True,
        return_tokens_as_token_ids=True,
        allowed_token_ids=ALLOWED_IDS,
    )
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    entries = (completion.raw["choices"][0].get("logprobs") or {}).get("content") or []
    tokens = [entry.get("token") for entry in entries if isinstance(entry, dict)]
    ids = [int(t.split(":", 1)[1]) for t in tokens if re.fullmatch(r"token_id:\d+", t or "")]
    if not ids or len(ids) != len(tokens) or not set(ids) <= set(ALLOWED_IDS):
        fail("allowed_tokens", tokens, f"every token one of {ALLOWED_IDS}")
    journal.record("allowed_tokens", sorted(set(ids)), f"within {ALLOWED_IDS}", ok=True)


@pytest.mark.case_id("openai_chat.bad_words")
def test_chat_bad_words(client, surface, model, ingress):
    """A word in bad_words never appears in the answer, even when the prompt asks for it."""
    require_option(ingress, "bad_words")
    body = surface.minimal_body(
        model.public_id, budget_for(model), BANNED_PROMPT, bad_words=[BANNED], temperature=0
    )
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    if BANNED in completion.text:
        fail("bad_words", completion.text, f"an answer without {BANNED!r}")
    journal.record("bad_words", completion.text, f"no {BANNED!r}", ok=True)


@pytest.mark.case_id("openai_chat.logit_bias")
def test_chat_logit_bias(client, surface, model, ingress):
    """A +100 logit bias selects its token at every step up to the limit."""
    require_option(ingress, "logit_bias")
    if "logprobs" not in ingress.capabilities.chat_options:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare Chat logprobs")
    body = surface.minimal_body(model.public_id, FORCED_STEPS, logprobs=True, logit_bias=FORCED)
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    entries = (completion.raw["choices"][0].get("logprobs") or {}).get("content") or []
    tokens = [entry.get("token") for entry in entries if isinstance(entry, dict)]
    count = completion.usage.completion_tokens
    if count != FORCED_STEPS or len(tokens) != count or len(set(tokens)) != 1:
        observed = {"tokens": tokens, "completion_tokens": count}
        fail("logit_bias", observed, f"one forced token at each of {FORCED_STEPS} steps")
    journal.record("logit_bias", tokens[0], "the forced token at every step", ok=True)
