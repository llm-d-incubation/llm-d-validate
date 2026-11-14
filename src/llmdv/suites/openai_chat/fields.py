"""Request bodies for the Chat field-disposition probes, one per name in `CHAT_FIELDS`.

Each probe adds one field, or one message shape, to an ordinary Chat request. The values are
the smallest valid ones the published types allow (openai SDK `chat/completion_create_params.py`
and the engine's `ChatCompletionRequest`), so a refusal can only be about the field. Probes
that only exist on a stream are listed in `STREAMED`.
"""

from __future__ import annotations

import uuid
from typing import Any

from ...surfaces.base import DEFAULT_PROMPT

# A one-pixel PNG, a two-word text file and a silent WAV header: valid encodings, nothing to read.
_PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_TEXT_FILE = "data:text/plain;base64,aGVsbG8gd29ybGQ="
_WAV = "UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA="
_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look a value up.",
        "parameters": {"type": "object", "properties": {"key": {"type": "string"}}},
    },
}


def _nonce() -> str:
    return f"llmdv-{uuid.uuid4().hex[:12]}"


def _messages(*before: dict[str, Any]) -> dict[str, Any]:
    return {"messages": [*before, {"role": "user", "content": DEFAULT_PROMPT}]}


def _parts(*parts: dict[str, Any]) -> dict[str, Any]:
    text = {"type": "text", "text": DEFAULT_PROMPT}
    return {"messages": [{"role": "user", "content": [text, *parts]}]}


def _probes() -> dict[str, dict[str, Any]]:
    """Fresh probe additions, so nonce-carrying fields never repeat between requests."""
    return {
        "role_system": _messages({"role": "system", "content": "You are concise."}),
        "role_developer": _messages({"role": "developer", "content": "You are concise."}),
        "role_assistant": _messages(
            {"role": "user", "content": "Hello."}, {"role": "assistant", "content": "Hello."}
        ),
        "assistant_refusal": _messages(
            {"role": "user", "content": "Hello."},
            {"role": "assistant", "content": None, "refusal": "I cannot help with that."},
        ),
        "role_tool": _messages(
            {"role": "user", "content": "Look up the key."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_llmdv_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"key": "a"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_llmdv_1", "content": "42"},
        ),
        "role_function": _messages(
            {"role": "user", "content": "Look up the key."},
            {"role": "function", "name": "lookup", "content": "42"},
        ),
        "message_name": {
            "messages": [{"role": "user", "name": "alice", "content": DEFAULT_PROMPT}]
        },
        "content_image_url": _parts({"type": "image_url", "image_url": {"url": _PNG}}),
        "content_file": _parts(
            {"type": "file", "file": {"filename": "note.txt", "file_data": _TEXT_FILE}}
        ),
        "content_input_audio": _parts(
            {"type": "input_audio", "input_audio": {"data": _WAV, "format": "wav"}}
        ),
        "add_generation_prompt": {"add_generation_prompt": True},
        "chat_template_kwargs": {"chat_template_kwargs": {}},
        "chat_template": {"chat_template": "{% for m in messages %}{{ m['content'] }}{% endfor %}"},
        "documents": {"documents": [{"title": "note", "text": "The sky is blue."}]},
        "truncation_side": {"truncation_side": "left"},
        "thinking_token_budget": {"thinking_token_budget": 64},
        "temperature": {"temperature": 0.7},
        "top_p": {"top_p": 0.9},
        "top_k": {"top_k": 20},
        "min_p": {"min_p": 0.05},
        "frequency_penalty": {"frequency_penalty": 0.5},
        "presence_penalty": {"presence_penalty": 0.5},
        "repetition_penalty": {"repetition_penalty": 1.05},
        "logit_bias": {"logit_bias": {"1000": -1}},
        "length_penalty": {"length_penalty": 1.0},
        "stop_token_ids": {"stop_token_ids": [0]},
        "seed": {"seed": 1234},
        "bad_words": {"bad_words": ["zebra"]},
        "use_beam_search": {"use_beam_search": True},
        "top_logprobs_alone": {"top_logprobs": 2},
        "prompt_logprobs": {"prompt_logprobs": 1},
        "include_obfuscation": {
            "stream_options": {"include_usage": True, "include_obfuscation": True}
        },
        "continuous_usage_stats": {
            "stream_options": {"include_usage": True, "continuous_usage_stats": True}
        },
        "stream_options_alone": {"stream_options": {"include_usage": True}},
        "stream_interval": {"stream_interval": 2},
        "response_format_text": {"response_format": {"type": "text"}},
        "include_reasoning": {"include_reasoning": True},
        "user": {"user": "llmdv-probe"},
        "metadata": {"metadata": {"llmdv": "probe"}},
        "store": {"store": False},
        "service_tier": {"service_tier": "auto"},
        "prediction": {"prediction": {"type": "content", "content": "ok"}},
        "modalities": {"modalities": ["text"]},
        "audio": {"audio": {"voice": "alloy", "format": "wav"}},
        "web_search_options": {"web_search_options": {}},
        "safety_identifier": {"safety_identifier": _nonce()},
        "prompt_cache_key": {"prompt_cache_key": _nonce()},
        "prompt_cache_retention": {"prompt_cache_retention": "in_memory"},
        "prompt_cache_options": {"prompt_cache_options": {"mode": "implicit"}},
        "verbosity": {"verbosity": "low"},
        "moderation": {"moderation": {"model": "omni-moderation-latest"}},
        "parallel_tool_calls": {"parallel_tool_calls": False},
        "tools": {"tools": [_TOOL], "tool_choice": "none"},
        "functions": {
            "functions": [_TOOL["function"]],
            "function_call": "none",
        },
        "skip_special_tokens": {"skip_special_tokens": True},
        "spaces_between_special_tokens": {"spaces_between_special_tokens": True},
        "add_special_tokens": {"add_special_tokens": False},
        "return_token_offsets": {"return_token_offsets": True},
        "return_prompt_text": {"return_prompt_text": True},
        "cache_salt": {"cache_salt": _nonce()},
        "request_id": {"request_id": _nonce()},
        "session_id": {"session_id": _nonce()},
        "structured_outputs": {"structured_outputs": {"choice": ["ok", "no"]}},
        "guided_json": {"guided_json": {"type": "object"}},
        "guided_choice": {"guided_choice": ["ok", "no"]},
        "guided_regex": {"guided_regex": "ok|no"},
        "guided_grammar": {"guided_grammar": 'root ::= "ok"'},
        "guided_decoding_backend": {"guided_decoding_backend": "auto"},
        "repetition_detection": {
            "repetition_detection": {"min_pattern_size": 2, "max_pattern_size": 8, "min_count": 4}
        },
        "logprob_token_ids": {"logprobs": True, "logprob_token_ids": [1000]},
        "allowed_token_ids": {"allowed_token_ids": [1000, 1001, 1002, 1003]},
        "routed_experts_prompt_start": {"routed_experts_prompt_start": 0},
        "system_only": {"messages": [{"role": "system", "content": DEFAULT_PROMPT}]},
        "assistant_last": {
            "messages": [
                {"role": "user", "content": DEFAULT_PROMPT},
                {"role": "assistant", "content": "Sure."},
            ]
        },
        "consecutive_users": _messages({"role": "user", "content": "Hello."}),
        "empty_user_content": {
            "messages": [
                {"role": "system", "content": DEFAULT_PROMPT},
                {"role": "user", "content": ""},
            ]
        },
        "system_content_parts": _messages(
            {"role": "system", "content": [{"type": "text", "text": "You are concise."}]}
        ),
        "assistant_content_parts": _messages(
            {"role": "user", "content": "Hello."},
            {"role": "assistant", "content": [{"type": "text", "text": "Hello."}]},
        ),
    }


# Stream options only mean something on a stream.
STREAMED = frozenset({"include_obfuscation", "continuous_usage_stats", "stream_interval"})

# Request fields the pinned SDK types as Optional, so null is a valid value. `max_tokens` is
# left out to keep the request bounded.
NULLABLE = (
    "audio",
    "frequency_penalty",
    "logit_bias",
    "logprobs",
    "max_completion_tokens",
    "metadata",
    "modalities",
    "moderation",
    "n",
    "prediction",
    "presence_penalty",
    "prompt_cache_key",
    "prompt_cache_retention",
    "reasoning_effort",
    "safety_identifier",
    "seed",
    "service_tier",
    "stop",
    "store",
    "stream",
    "stream_options",
    "temperature",
    "top_logprobs",
    "top_p",
    "verbosity",
)


def probe(name: str) -> dict[str, Any]:
    """The fields the named probe adds to an ordinary request."""
    return _probes()[name]


def names() -> tuple[str, ...]:
    return tuple(_probes())
