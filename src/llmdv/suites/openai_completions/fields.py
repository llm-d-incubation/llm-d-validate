"""Request bodies for the Completions field-disposition probes, one per `COMPLETIONS_FIELDS` name.

The values are the smallest valid ones in openai SDK `completion_create_params.py` and the
engine's `CompletionRequest`, so a refusal can only be about the field.
"""

from __future__ import annotations

import uuid
from typing import Any


def _probes() -> dict[str, dict[str, Any]]:
    return {
        "suffix": {"suffix": " end"},
        "best_of": {"best_of": 1},
        "temperature": {"temperature": 0.7},
        "top_p": {"top_p": 0.9},
        "frequency_penalty": {"frequency_penalty": 0.5},
        "presence_penalty": {"presence_penalty": 0.5},
        "seed": {"seed": 1234},
        "logit_bias": {"logit_bias": {"1000": -1}},
        "user": {"user": "llmdv-probe"},
        "top_k": {"top_k": 20},
        "min_p": {"min_p": 0.05},
        "repetition_penalty": {"repetition_penalty": 1.05},
        "min_tokens": {"min_tokens": 1},
        "ignore_eos": {"ignore_eos": True},
        "include_stop_str_in_output": {"include_stop_str_in_output": True},
        "skip_special_tokens": {"skip_special_tokens": True},
        "return_token_ids": {"return_token_ids": True},
        "prompt_logprobs": {"prompt_logprobs": 1},
        "cache_salt": {"cache_salt": f"llmdv-{uuid.uuid4().hex[:12]}"},
        "response_format_json_object": {"response_format": {"type": "json_object"}},
        "structured_outputs": {"structured_outputs": {"choice": ["ok", "no"]}},
        "stream_options_alone": {"stream_options": {"include_usage": True}},
        "use_beam_search": {"use_beam_search": True},
        "length_penalty": {"length_penalty": 1.0},
        "stop_token_ids": {"stop_token_ids": [0]},
        "spaces_between_special_tokens": {"spaces_between_special_tokens": True},
        "truncate_prompt_tokens": {"truncate_prompt_tokens": 64},
        "truncation_side": {"truncation_side": "left"},
        "allowed_token_ids": {"allowed_token_ids": [1000, 1001, 1002, 1003]},
        "logprob_token_ids": {"logprobs": 1, "logprob_token_ids": [1000]},
        "bad_words": {"bad_words": ["zebra"]},
        "add_special_tokens": {"add_special_tokens": False},
        "request_id": {"request_id": f"llmdv-{uuid.uuid4().hex[:12]}"},
        "session_id": {"session_id": f"llmdv-{uuid.uuid4().hex[:12]}"},
        "return_tokens_as_token_ids": {"logprobs": 1, "return_tokens_as_token_ids": True},
        "routed_experts_prompt_start": {"routed_experts_prompt_start": 0},
        "return_token_offsets": {"return_token_offsets": True},
        "repetition_detection": {
            "repetition_detection": {"min_pattern_size": 2, "max_pattern_size": 8, "min_count": 4}
        },
        "thinking_token_budget": {"thinking_token_budget": 64},
        "stream_interval": {"stream_interval": 2},
    }


# Stream options only mean something on a stream.
STREAMED = frozenset({"stream_interval"})

# Request fields the pinned SDK types as Optional, so null is a valid value. `max_tokens` is
# left out to keep the request bounded.
NULLABLE = (
    "best_of",
    "echo",
    "frequency_penalty",
    "logit_bias",
    "logprobs",
    "n",
    "presence_penalty",
    "seed",
    "stop",
    "stream",
    "stream_options",
    "suffix",
    "temperature",
    "top_p",
)


def probe(name: str) -> dict[str, Any]:
    return _probes()[name]


def names() -> tuple[str, ...]:
    return tuple(_probes())
