"""Token counting covers every part of the input a generation renders: system, tools, thinking."""

import pytest

from ...assertions.context import assert_prompt_grows
from ...assertions.messages import assert_messages_token_count
from ...evidence import journal
from ...surfaces.base import TOOL_NAME
from ..conftest import check_json_response

pytestmark = [
    pytest.mark.surface("messages"),
    pytest.mark.needs("count_tokens", "messages_checks:count_tokens_parts"),
]

# Long enough that the difference cannot be mistaken for per-part template overhead.
FILLER_WORDS = 200
PROMPT = "Reply with ok."


def _with_reply(base, blocks):
    return {
        **base,
        "messages": [
            *base["messages"],
            {"role": "assistant", "content": blocks},
            {"role": "user", "content": "Thanks. Reply with ok."},
        ],
    }


def _extended(base, part, ingress):
    if part == "system":
        return {**base, "system": "Project rules. " + "rule " * FILLER_WORDS}
    if part == "tools":
        tool = {
            "name": TOOL_NAME,
            "description": "Look up a project rule. " + "rule " * FILLER_WORDS,
            "input_schema": {"type": "object", "properties": {"rule": {"type": "string"}}},
        }
        return {**base, "tools": [tool]}
    if part == "prior_thinking":
        thought = {"type": "thinking", "thinking": "step " * FILLER_WORDS, "signature": "llmdv-sig"}
        return _with_reply(base, [thought, {"type": "text", "text": "ok"}])
    declared = ingress.capabilities.messages_thinking
    mode = next((m for m in ("adaptive", "enabled", "disabled") if m in declared), None)
    if mode is None:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no thinking mode")
    thinking = {"type": mode, **({"budget_tokens": 1024} if mode == "enabled" else {})}
    return {**base, "thinking": thinking}


@pytest.mark.case_id("anthropic_messages.count_tokens_parts")
@pytest.mark.parametrize("part", ["system", "tools", "thinking_config", "prior_thinking"])
def test_token_count_covers_each_part(client, surface, model, ingress, part):
    base = surface.count_tokens_body(model.public_id, PROMPT)
    extended = _extended(base, part, ingress)
    if part == "prior_thinking":
        base = _with_reply(base, [{"type": "text", "text": "ok"}])
    counts = []
    for body in (base, extended):
        exchange = client.post(surface.count_tokens_path, json_body=body)
        counts.append(assert_messages_token_count(check_json_response(exchange, ingress)))
    if part == "thinking_config":
        # A thinking configuration may add tokens or none; it must be accepted and counted.
        journal.record("thinking_config_counted", {"without": counts[0], "with": counts[1]})
        return
    if part == "prior_thinking":
        # Models that keep earlier thinking count it; models that strip it count the same.
        ok = counts[1] >= counts[0]
        journal.record(
            "prior_thinking_counted",
            {"without": counts[0], "with": counts[1]},
            "no fewer tokens with an earlier thinking block",
            ok=ok,
        )
        assert ok, f"adding earlier thinking lowered the count ({counts[0]} -> {counts[1]})"
        return
    assert_prompt_grows(f"count_tokens_{part}", counts[0], counts[1], FILLER_WORDS // 2)
