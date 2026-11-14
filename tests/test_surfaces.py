"""Surface parsing, against the exact bodies a live fleet returned.

The inline-<think> case is the important one. A fleet served without --reasoning-parser puts
reasoning in `content` and leaves `reasoning_content` null, so a naive "content is non-empty"
assertion passes for the wrong reason and a "reasoning_content is populated" assertion fails a
healthy stack.
"""

from __future__ import annotations

import pytest

from llmdv.surfaces import get, register
from llmdv.surfaces.base import (
    REQUEST_ERROR_CLASSES,
    SURFACE_PATHS,
    TOOL_ERROR_CLASSES,
    probe_request,
)

CHAT = get("chat_completions")


def test_completions_stream_assertion_is_implemented():
    from llmdv.http.sse import read

    assert get("completions").has_stream_lifecycle
    with pytest.raises(AssertionError):
        get("completions").assert_stream(read([]))


# Captured verbatim from a live router (no reasoning parser configured).
LIVE_INLINE_THINKING = {
    "object": "chat.completion",
    "model": "Qwen/Qwen3-32B",
    "choices": [
        {
            "finish_reason": "length",
            "message": {
                "role": "assistant",
                "content": '<think>\nOkay, the user wants me to say "ok". Let',
                "reasoning_content": None,
            },
        }
    ],
    "usage": {"prompt_tokens": 14, "completion_tokens": 64, "total_tokens": 78},
}

LIVE_UNKNOWN_MODEL = {
    "error": {
        "message": "The model `llmdv-nonexistent-abc` does not exist.",
        "type": "NotFoundError",
        "param": "model",
        "code": 404,
    }
}


def test_inline_thinking_is_split_out_of_content():
    completion = CHAT.parse_completion(LIVE_INLINE_THINKING)
    assert completion.text == ""  # content was ONLY a think block
    assert "the user wants me to say" in completion.reasoning_text
    assert completion.has_output is True  # reasoning counts as output


def test_separate_reasoning_field_is_preferred_when_present():
    body = {
        "object": "chat.completion",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "ok",
                    "reasoning_content": "deliberating",
                },
            }
        ],
    }
    completion = CHAT.parse_completion(body)
    assert completion.text == "ok"
    assert completion.reasoning_text == "deliberating"


def test_unterminated_think_block_still_splits():
    """A reply truncated by max_tokens mid-thought is the common case, not an edge case."""
    body = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"role": "assistant", "content": "<think>half a thou"},
            }
        ]
    }
    completion = CHAT.parse_completion(body)
    assert completion.text == ""
    assert completion.reasoning_text == "half a thou"


def test_usage_consistency_and_its_absence():
    assert CHAT.parse_completion(LIVE_INLINE_THINKING).usage.is_self_consistent
    bad = {
        "choices": [{"message": {}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 99},
    }
    assert not CHAT.parse_completion(bad).usage.is_self_consistent
    # Missing fields are a separate assertion, so they must not read as inconsistent.
    partial = {"choices": [{"message": {}}], "usage": {"prompt_tokens": 1}}
    assert CHAT.parse_completion(partial).usage.is_self_consistent


def test_openai_error_envelope_is_parsed():
    envelope = CHAT.parse_error(LIVE_UNKNOWN_MODEL)
    assert envelope is not None
    assert envelope.type == "NotFoundError"
    assert envelope.code == 404
    assert envelope.param == "model"


def test_flattened_error_envelope_is_accepted():
    """Some layers flatten the envelope; the assertion is about structure, not nesting."""
    envelope = CHAT.parse_error({"message": "nope", "type": "BadRequest"})
    assert envelope is not None and envelope.message == "nope"


def test_a_success_body_is_not_an_error_envelope():
    assert CHAT.parse_error(LIVE_INLINE_THINKING) is None
    assert CHAT.parse_error("not json at all") is None


def test_looks_like_success_detects_a_generation():
    assert CHAT.looks_like_success(LIVE_INLINE_THINKING)
    assert not CHAT.looks_like_success(LIVE_UNKNOWN_MODEL)
    assert not CHAT.looks_like_success({"choices": []})


def test_probe_bodies_are_shape_valid_for_every_known_surface():
    """An empty {} body is unusable: the router validates bodies before resolving routes."""
    for name in SURFACE_PATHS:
        body, headers = probe_request(name, "some/model")
        assert body.get("model") == "some/model", name
        if name in ("messages", "messages_render"):
            assert headers.get("anthropic-version"), name
        if name == "responses":
            assert "input" in body, name
        elif name in ("completions", "completions_render"):
            assert "prompt" in body, name
        elif name in ("chat_completions", "chat_render"):
            assert "messages" in body, name


def test_chat_invalid_bodies_break_the_named_thing_and_nothing_else():
    valid = CHAT.minimal_body("m", max_tokens=8)
    for error_class in REQUEST_ERROR_CLASSES + TOOL_ERROR_CLASSES:
        body = CHAT.invalid_body("m", error_class)
        assert body["model"] == "m", error_class
        changed = {k for k in valid.keys() | body.keys() if valid.get(k) != body.get(k)}
        assert len(changed) == 1, (error_class, changed)

    assert "messages" not in CHAT.invalid_body("m", "missing_required_field")
    assert CHAT.invalid_body("m", "invalid_token_limit")["max_tokens"] < 0
    assert CHAT.invalid_body("m", "wrong_field_type", stream=True)["stream"] is True


def test_an_error_class_a_surface_cannot_express_is_refused_loudly():
    with pytest.raises(ValueError):
        CHAT.invalid_body("m", "unknown_model")


def test_minimal_body_carries_the_prompt_in_the_surfaces_own_field():
    assert CHAT.minimal_body("m", prompt="hello")["messages"][0]["content"] == "hello"


def test_a_surface_declaring_an_unknown_feature_is_refused():
    class Misspelt(type(CHAT)):
        name = "misspelt"
        features = frozenset({"tool"})

    with pytest.raises(ValueError, match="unknown features"):
        register(Misspelt())
