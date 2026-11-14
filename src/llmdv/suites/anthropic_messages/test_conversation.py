"""Conversation content a layer can drop while every response still looks correct.

The prompt-growth checks are necessary rather than sufficient. A template that MOVES the entry
rather than dropping it -- to the top of the prompt, say -- grows the input by the same number of
tokens and passes, while destroying the reusable prefix behind it. Growth proves the content
reached the model, not that it reached it in the right place; the render-side cases in COVERAGE
section 9 are what would catch the move, and they need a fleet that serves a render surface.
"""

import uuid
from dataclasses import replace

import pytest

from ...assertions.context import (
    assert_count_matches_usage,
    assert_marker_followed,
    assert_prompt_grows,
    prompt_size,
)
from ...assertions.messages import assert_messages_token_count
from ...assertions.thinking import assert_thinking_blocks
from ..conftest import budget_for, check_basic_completion, check_json_response
from ..tools.helpers import tool_schema
from .helpers import mid_system_request

pytestmark = pytest.mark.surface("messages")

# Long enough that the difference cannot be mistaken for per-message template overhead.
FILLER_WORDS = 300
MIN_GROWTH = FILLER_WORDS // 2


def _thinking_mode(ingress) -> str:
    declared = ingress.capabilities.messages_thinking
    mode = next((m for m in ("enabled", "adaptive") if m in declared), None)
    if mode is None:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no thinking mode to send back")
    return mode


def _generate(client, surface, model, ingress, body):
    return check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )


@pytest.mark.needs("messages_checks:mid_conversation_system")
@pytest.mark.case_id("anthropic_messages.mid_conversation_system_followed")
def test_mid_conversation_system_is_followed(client, surface, model, ingress):
    marker = uuid.uuid4().hex[:12]
    body = mid_system_request(
        surface, model, f"The code word is {marker}.", budget_for(model, floor=256)
    )
    completion = _generate(client, surface, model, ingress, body)
    assert_marker_followed("mid_conversation_system_followed", completion, marker)


@pytest.mark.needs("messages_checks:mid_conversation_system", "messages_checks:prompt_accounting")
@pytest.mark.case_id("anthropic_messages.mid_conversation_system_in_prompt")
def test_mid_conversation_system_reaches_prompt(client, surface, model, ingress):
    note = "Reference note. " + "item " * FILLER_WORDS
    sizes = [
        prompt_size(
            surface,
            _generate(
                client,
                surface,
                model,
                ingress,
                mid_system_request(surface, model, content, budget_for(model)),
            ),
        )
        for content in (None, note)
    ]
    assert_prompt_grows("mid_conversation_system_tokens", sizes[0], sizes[1], MIN_GROWTH)


@pytest.mark.needs("messages_checks:prompt_accounting")
@pytest.mark.case_id("anthropic_messages.system_blocks_in_prompt")
def test_later_system_blocks_reach_prompt(client, surface, model, ingress):
    first = "You are a helpful assistant."
    later = "Project rules. " + "rule " * FILLER_WORDS
    sizes = [
        prompt_size(
            surface,
            _generate(
                client,
                surface,
                model,
                ingress,
                surface.system_blocks_body(model.public_id, blocks, budget_for(model)),
            ),
        )
        for blocks in ([first], [first, later])
    ]
    assert_prompt_grows("system_blocks_tokens", sizes[0], sizes[1], MIN_GROWTH)


@pytest.mark.needs("messages_checks:prompt_accounting")
@pytest.mark.case_id("anthropic_messages.thinking_retained")
def test_earlier_thinking_reaches_prompt(client, surface, model, ingress):
    mode = _thinking_mode(ingress)
    first_body = surface.thinking_body(model.public_id, mode=mode)
    first = _generate(client, surface, model, ingress, first_body)
    assert_thinking_blocks(first, mode)
    blocks = first.raw["content"]
    answer = [b for b in blocks if b.get("type") not in ("thinking", "redacted_thinking")]
    if len(answer) == len(blocks):
        pytest.skip("[requires_instrumentation] the model returned no thinking block to send back")
    sizes = [
        prompt_size(
            surface,
            _generate(
                client,
                surface,
                model,
                ingress,
                surface.continuation_body(first_body, content, "Thanks. Reply ok."),
            ),
        )
        for content in (answer, blocks)
    ]
    thinking_chars = sum(len(b.get("thinking", "")) for b in blocks if b.get("type") == "thinking")
    assert_prompt_grows(
        "thinking_retained_tokens", sizes[0], sizes[1], max(1, thinking_chars // 16)
    )


@pytest.mark.needs("tools", "messages_checks:prompt_accounting")
@pytest.mark.case_id("anthropic_messages.thinking_survives_tool_result")
def test_thinking_before_a_tool_call_reaches_prompt(client, surface, model, ingress):
    mode = _thinking_mode(ingress)
    body = surface.thinking_tool_body(
        model.public_id, tool_schema(), mode, budget_for(model, floor=256)
    )
    first = _generate(client, surface, model, ingress, body)
    blocks = first.raw["content"]
    kept = [b for b in blocks if b.get("type") not in ("thinking", "redacted_thinking")]
    if len(kept) == len(blocks) or not first.tool_calls:
        # The call is not forced and adaptive thinking is optional, so this can happen.
        pytest.skip("[requires_instrumentation] the model returned no thinking beside a tool call")
    result = uuid.uuid4().hex[:12]
    sizes = [
        prompt_size(
            surface,
            _generate(
                client,
                surface,
                model,
                ingress,
                surface.tool_result_body(
                    body, replace(first, raw={**first.raw, "content": content}), result
                ),
            ),
        )
        for content in (kept, blocks)
    ]
    thinking_chars = sum(len(b.get("thinking", "")) for b in blocks if b.get("type") == "thinking")
    assert_prompt_grows(
        "thinking_survives_tool_result_tokens", sizes[0], sizes[1], max(1, thinking_chars // 16)
    )


@pytest.mark.needs("count_tokens", "messages_checks:prompt_accounting")
@pytest.mark.case_id("anthropic_messages.token_count_matches_usage")
def test_token_count_matches_generation_usage(client, surface, model, ingress):
    prompt = "Reference text. " + "item " * FILLER_WORDS + "Reply ok."
    system = "You are a helpful assistant. " + "rule " * FILLER_WORDS
    count_body = surface.count_tokens_body(model.public_id, prompt, system=system)
    exchange = client.post(surface.count_tokens_path, json_body=count_body)
    counted = assert_messages_token_count(check_json_response(exchange, ingress))
    body = surface.minimal_body(model.public_id, budget_for(model), prompt, system=system)
    used = prompt_size(surface, _generate(client, surface, model, ingress, body))
    assert_count_matches_usage(counted, used)
