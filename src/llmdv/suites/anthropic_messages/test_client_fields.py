"""Request fields a client release adds: they must be honoured, not just tolerated."""

import pytest

from ...assertions.context import assert_effort_effect
from ...evidence import journal
from ...surfaces.messages import CONTEXT_MANAGEMENT_BETA
from ..conftest import budget_for, check_basic_completion

pytestmark = pytest.mark.surface("messages")

# Hard enough that a model honouring effort reasons visibly longer at high than at low.
EFFORT_PROMPT = "How many integers between 1 and 5000 are divisible by 7 or 11 but not 13?"
# One generation per level is too noisy to compare; these are samples, not retries.
EFFORT_SAMPLES = 3


@pytest.mark.case_id("anthropic_messages.beta_header")
def test_declared_beta_values_are_accepted(client, surface, model, ingress):
    betas = ingress.capabilities.anthropic_betas
    if not betas:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no anthropic-beta values")
    journal.record("anthropic_beta_sent", list(betas))
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model))
    exchange = client.post(
        surface.path, json_body=body, headers={"anthropic-beta": ",".join(betas)}
    )
    check_basic_completion(exchange, surface, model, ingress)


@pytest.mark.needs("messages_checks:context_management")
@pytest.mark.case_id("anthropic_messages.context_management")
def test_context_management_is_accepted(client, surface, model, ingress):
    body = surface.context_management_body(model.public_id, budget_for(model))
    exchange = client.post(
        surface.path, json_body=body, headers={"anthropic-beta": CONTEXT_MANAGEMENT_BETA}
    )
    check_basic_completion(exchange, surface, model, ingress)


@pytest.mark.needs("messages_checks:effort_effect")
@pytest.mark.case_id("anthropic_messages.effort_effect")
def test_effort_changes_output_size(client, surface, model, ingress):
    declared = ingress.capabilities.messages_efforts
    if not {"low", "high"} <= set(declared):
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} does not declare both low and high effort"
        )
    mode = "adaptive" if "adaptive" in ingress.capabilities.messages_thinking else None
    # Room to reason at high effort, without asking a small model for more than it has.
    budget = min(8192, max(1024, model.max_model_len // 4))
    tokens: dict[str, list[int]] = {"low": [], "high": []}
    for _ in range(EFFORT_SAMPLES):
        for effort in ("low", "high"):
            body = surface.thinking_body(
                model.public_id, mode=mode, effort=effort, prompt=EFFORT_PROMPT, max_tokens=budget
            )
            completion = check_basic_completion(
                client.post(surface.path, json_body=body), surface, model, ingress
            )
            if completion.finish_reason == "max_tokens":
                pytest.skip(
                    f"[requires_instrumentation] a sample reached the {budget}-token output "
                    "budget, so output size no longer reflects effort"
                )
            tokens[effort].append(completion.usage.completion_tokens or 0)
    assert_effort_effect(tokens["low"], tokens["high"])
