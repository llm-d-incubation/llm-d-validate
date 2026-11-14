"""Token limits and stop strings end a Chat generation exactly where the client asked.

Clients budget cost and latency on `max_tokens`, cut structured output with `stop`, and newer
SDKs send only `max_completion_tokens`. On a disaggregated fleet the prefill leg runs with a
rewritten limit, so a limit that survives one hop and not the other shows up only as a count.
Each case checks the native count and finish reason. vLLM also names the stop string that
matched in the choice's `stop_reason`; a client that sends several stop strings reads it to
learn which one ended the reply.

A stop string applies to the answer. With reasoning on, an engine may match it inside the
reasoning and end the request before any answer; that combination has no contract (OpenAI
advises against `stop` with reasoning models), so the stop cases turn reasoning off on a model
that reasons by default, with the quiet effort the profile declares.
"""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...evidence import journal
from ..conftest import budget_for, check_completion
from ..openai_contract import (
    assemble_stream,
    check_generation,
    check_stop_reason,
    reasoning_off,
    require_contract,
    require_option,
    stream_frames,
)

pytestmark = pytest.mark.surface("chat_completions")

ESSAY_PROMPT = "Write a long essay about rivers."
COUNT_PROMPT = "Count from 1 to 10 separated by commas."
STOP = ", 5"
# Requested limits: one field, the other, and both with the non-deprecated one lower.
LIMITS = {
    "max_tokens": ({"max_tokens": 7}, 7),
    "max_completion_tokens": ({"max_completion_tokens": 7}, 7),
    "both": ({"max_tokens": 9, "max_completion_tokens": 5}, 5),
}


def _check_cut(completion, expected: int) -> None:
    observed = {
        "completion_tokens": completion.usage.completion_tokens,
        "finish_reason": completion.finish_reason,
    }
    wanted = f"exactly {expected} completion tokens ending at length"
    if observed != {"completion_tokens": expected, "finish_reason": "length"}:
        fail("token_limit", observed, wanted)
    journal.record("token_limit", observed, wanted, ok=True)


@pytest.mark.case_id("openai_chat.token_limit")
@pytest.mark.parametrize("fields", list(LIMITS))
def test_chat_token_limit(client, surface, model, ingress, fields):
    require_contract(ingress, surface)
    limits, expected = LIMITS[fields]
    body = surface.minimal_body(model.public_id, prompt=ESSAY_PROMPT)
    body.pop("max_tokens")
    body.update(limits)
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    _check_cut(completion, expected)


@pytest.mark.case_id("openai_chat.single_token")
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_chat_single_token(client, surface, model, ingress, stream):
    """One token back, as the prefill leg of a disaggregated fleet itself runs."""
    require_contract(ingress, surface)
    if stream:
        body = surface.stream_body(model.public_id, 1, include_usage=True, prompt=ESSAY_PROMPT)
        completion = assemble_stream(surface, stream_frames(client, surface, body, ingress))
    else:
        body = surface.minimal_body(model.public_id, 1, prompt=ESSAY_PROMPT)
        completion = check_generation(
            client.post(surface.path, json_body=body), surface, model, ingress
        )
    _check_cut(completion, 1)


def _check_stop(completion, included: bool) -> None:
    text = completion.text
    observed = {"text": text, "finish_reason": completion.finish_reason}
    if included:
        ok = text.rstrip().endswith(STOP) and "6" not in text
        wanted = f"text ending with {STOP!r}, finish stop"
    else:
        ok = bool(text.strip()) and STOP not in text and "10" not in text
        wanted = f"text cut before {STOP!r} without it, finish stop"
    if not (ok and completion.finish_reason == "stop"):
        fail("stop_sequence", observed, wanted)
    journal.record("stop_sequence", observed, wanted, ok=True)


def quiet(ingress, model) -> dict:
    """Reasoning off where the model reasons by default, so the stop string meets the answer.

    A model with no off state is sent its lowest effort, and the stop string may meet reasoning.
    """
    off = reasoning_off(ingress, model)
    journal.record("reasoning_off", off or "not needed: the model does not reason by default")
    return off


@pytest.mark.case_id("openai_chat.stop_sequence")
@pytest.mark.parametrize("shape", ["string", "list", "sse", "included"])
def test_chat_stop_sequence(client, surface, model, ingress, shape):
    require_contract(ingress, surface)
    if shape == "included":
        require_option(ingress, "include_stop_str_in_output")
    stop = [STOP, "llmdv-never-generated"] if shape == "list" else STOP
    extra = {"stop": stop, **quiet(ingress, model)}
    if shape == "included":
        extra["include_stop_str_in_output"] = True
    budget = budget_for(model)
    if shape == "sse":
        body = surface.stream_body(
            model.public_id, budget, include_usage=True, prompt=COUNT_PROMPT, **extra
        )
        completion = assemble_stream(surface, stream_frames(client, surface, body, ingress))
        check_completion(completion, surface, model)
    else:
        body = surface.minimal_body(model.public_id, budget, COUNT_PROMPT, **extra)
        completion = check_generation(
            client.post(surface.path, json_body=body), surface, model, ingress
        )
    _check_stop(completion, included=shape == "included")


@pytest.mark.case_id("openai_chat.stop_reason")
def test_chat_stop_reason(client, surface, model, ingress):
    """The choice names the stop string that ended the reply."""
    require_contract(ingress, surface)
    require_option(ingress, "stop_reason")
    stop = [STOP, "llmdv-never-generated"]
    body = surface.minimal_body(
        model.public_id, budget_for(model), COUNT_PROMPT, stop=stop, **quiet(ingress, model)
    )
    completion = check_generation(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    check_stop_reason(completion, STOP)
