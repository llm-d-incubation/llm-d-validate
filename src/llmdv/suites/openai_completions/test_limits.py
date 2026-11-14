"""Token limits and stop strings on the prompt API end the text where the client asked.

vLLM names the stop string that matched in the choice's `stop_reason`.
"""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...evidence import journal
from ..conftest import budget_for
from ..openai_contract import (
    check_generation,
    check_stop_reason,
    require_contract,
    require_option,
)

pytestmark = pytest.mark.surface("completions")

STOP = ", 5"


@pytest.mark.case_id("openai_completions.token_limit")
def test_completions_token_limit(client, surface, model, ingress):
    require_contract(ingress, surface)
    body = surface.minimal_body(model.public_id, 7, "Write a long essay about rivers.\n\n")
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    observed = {
        "completion_tokens": completion.usage.completion_tokens,
        "finish_reason": completion.finish_reason,
    }
    if observed != {"completion_tokens": 7, "finish_reason": "length"}:
        fail("token_limit", observed, "exactly 7 completion tokens ending at length")
    journal.record("token_limit", observed, "exactly 7 completion tokens ending at length", ok=True)


@pytest.mark.case_id("openai_completions.stop_sequence")
def test_completions_stop_sequence(client, surface, model, ingress):
    require_contract(ingress, surface)
    prompt = "Count from 1 to 10 separated by commas: 1, 2, 3,"
    body = surface.minimal_body(model.public_id, budget_for(model), prompt, stop=[STOP])
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    raw_text = completion.raw["choices"][0].get("text") or ""
    observed = {"text": raw_text, "finish_reason": completion.finish_reason}
    ok = completion.finish_reason == "stop" and STOP not in raw_text and "10" not in raw_text
    if not ok:
        fail("stop_sequence", observed, f"text without {STOP!r}, finish stop")
    journal.record("stop_sequence", observed, f"text without {STOP!r}, finish stop", ok=True)


@pytest.mark.case_id("openai_completions.stop_reason")
def test_completions_stop_reason(client, surface, model, ingress):
    """The choice names the stop string that ended the text."""
    require_contract(ingress, surface)
    require_option(ingress, "stop_reason")
    prompt = "Count from 1 to 10 separated by commas: 1, 2, 3,"
    stop = [STOP, "llmdv-never-generated"]
    body = surface.minimal_body(model.public_id, budget_for(model), prompt, stop=stop)
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    check_stop_reason(completion, STOP)
