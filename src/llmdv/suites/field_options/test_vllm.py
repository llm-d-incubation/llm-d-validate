"""Bounded controls make ignored vendor options observable."""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...assertions.vllm_options import assert_token_ids
from ...evidence import journal
from ...surfaces.base import Completion
from ..conftest import check_basic_completion


@pytest.mark.surface_generic("vllm_options")
@pytest.mark.parametrize(
    "option", ["return_token_ids", "min_tokens", "ignore_eos", "truncate_prompt_tokens"]
)
@pytest.mark.case_id("field_options.vllm_observable")
def test_vllm_observable_options(client, surface, model, ingress, option):
    if not ingress.capabilities.supports_option(option):
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare {option}")
    if option not in surface.observable_vllm_options:
        pytest.skip(f"[not_implemented] {option} has no observable probe on {surface.name}")

    def generate(**options) -> Completion:
        body = surface.minimal_body(model.public_id, **options)
        result = check_basic_completion(
            client.post(surface.path, json_body=body), surface, model, ingress
        )
        for name, count in (
            ("prompt", result.usage.prompt_tokens),
            ("output", result.usage.completion_tokens),
        ):
            if not (type(count) is int and count >= 0):
                fail(f"{name}_token_count", count, "non-negative integer")
            journal.record(f"{name}_token_count", count, "non-negative integer", ok=True)
        return result

    if option == "return_token_ids":
        result = generate(max_tokens=16, return_token_ids=True)
        assert_token_ids(result.raw, result.usage)
    elif option == "truncate_prompt_tokens":
        prompt = "word " * 64
        baseline = generate(max_tokens=8, prompt=prompt)
        if not (baseline.usage.prompt_tokens > 8):
            fail("truncation_control", baseline.usage.prompt_tokens, "greater than 8")
        journal.record(
            "truncation_control", baseline.usage.prompt_tokens, "greater than 8", ok=True
        )
        result = generate(max_tokens=8, prompt=prompt, truncate_prompt_tokens=8)
        if not (0 < result.usage.prompt_tokens <= 8):
            fail("truncated_prompt_tokens", result.usage.prompt_tokens, "1..8")
        journal.record("truncated_prompt_tokens", result.usage.prompt_tokens, "1..8", ok=True)
    else:
        baseline = generate(max_tokens=64)
        if baseline.finish_reason != "stop" or baseline.usage.completion_tokens >= 64:
            pytest.skip(
                "[requires_load] bounded control did not stop early; "
                "no option effect can be established"
            )
        minimum = min(64, baseline.usage.completion_tokens + 8)
        result = generate(
            max_tokens=64,
            **({"min_tokens": minimum} if option == "min_tokens" else {"ignore_eos": True}),
        )
        count = result.usage.completion_tokens
        if option == "min_tokens":
            if not (minimum <= count <= 64):
                fail("minimum_output_tokens", count, f"{minimum}..64")
            journal.record("minimum_output_tokens", count, f"{minimum}..64", ok=True)
        else:
            if not (count == 64 and result.finish_reason == "length"):
                fail(
                    "ignored_eos_output",
                    {"tokens": count, "finish": result.finish_reason},
                    "64 tokens ending at length",
                )
            journal.record(
                "ignored_eos_output",
                {"tokens": count, "finish": result.finish_reason},
                "64 tokens ending at length",
                ok=True,
            )
