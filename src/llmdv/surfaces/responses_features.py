"""Optional Responses request features: the wire shape of each, and what proves it was honored.

A fleet declares each feature `accepted`, `ignored` or `rejected` under
`capabilities.responses_features`. The case in `suites/openai_responses/test_features.py`
sends one feature per request on an otherwise minimal body.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# A 1x1 PNG: the smallest valid image payload.
_PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4"
    "DwABBAEAX+XDSwAAAABJRU5ErkJggg=="
)
CODE_WORD = "basalt"
FILE_TEXT = f"The code word is {CODE_WORD}."
QUESTION = "What is the code word? It is in the file. Reply with the code word only."
ANY_STRING = object()  # an echo that may differ from the request, as long as it is a string


@dataclass(frozen=True)
class Feature:
    """One optional feature.

    `body` returns the fields to add, given a fresh marker. `echo` names the response field
    that must carry `expected` when the feature is declared accepted; a mapping is matched on
    the requested keys only. `calls` are output item types showing a built-in tool ran.
    """

    body: Callable[[str], dict[str, Any]]
    echo: str | None = None
    expected: Callable[[str], Any] | None = None
    calls: tuple[str, ...] = ()
    effect: str | None = None
    reference: bool = False  # resolves stored state: a fresh reference cannot be accepted
    stream: bool = False  # sent as a stream; the terminal snapshot is the response


def _echoed(name: str, value: Any) -> Feature:
    return Feature(lambda _: {name: value}, echo=name, expected=lambda _: value)


def _marked(name: str) -> Feature:
    return Feature(lambda m: {name: f"llmdv-{m}"}, echo=name, expected=lambda m: f"llmdv-{m}")


def _tool(definition: dict[str, Any], *calls: str) -> Feature:
    return Feature(lambda _: {"tools": [definition]}, calls=calls)


def _input(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "message", "role": "user", "content": parts}]


# A function tool added mid-conversation by an `additional_tools` input item.
_LOOKUP = {
    "type": "function",
    "name": "get_code_word",
    "description": "Returns the current code word.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
_FILE = "data:text/plain;base64," + base64.b64encode(FILE_TEXT.encode()).decode()

FEATURES: dict[str, Feature] = {
    "temperature": _echoed("temperature", 0.5),
    # Sent with a sampling temperature: at the minimal body's temperature 0 decoding is greedy,
    # and the engine reports the top_p it applied, 1.0 (vLLM's SamplingParams).
    "top_p": Feature(
        lambda _: {"top_p": 0.5, "temperature": 1.0}, echo="top_p", expected=lambda _: 0.5
    ),
    "max_tool_calls": _echoed("max_tool_calls", 3),
    "parallel_tool_calls": _echoed("parallel_tool_calls", False),
    "background_false": _echoed("background", False),
    "user": _marked("user"),
    "safety_identifier": _marked("safety_identifier"),
    "prompt_cache_key": _marked("prompt_cache_key"),
    "prompt_cache_retention": _echoed("prompt_cache_retention", "24h"),
    "prompt_cache_options": _echoed("prompt_cache_options", {"mode": "implicit", "ttl": "30m"}),
    "service_tier": Feature(
        lambda _: {"service_tier": "default"}, echo="service_tier", expected=lambda _: ANY_STRING
    ),
    "truncation_auto": _echoed("truncation", "auto"),
    "text_format_text": _echoed("text", {"format": {"type": "text"}}),
    "text_verbosity": _echoed("text", {"verbosity": "low"}),
    "reasoning_summary": Feature(
        lambda _: {"reasoning": {"summary": "detailed"}},
        echo="reasoning",
        expected=lambda _: {"summary": "detailed"},
        effect="summary",
    ),
    "reasoning_context": Feature(lambda _: {"reasoning": {"context": "current_turn"}}),
    "encrypted_reasoning": Feature(
        lambda _: {"include": ["reasoning.encrypted_content"]}, effect="encrypted_content"
    ),
    "include_tool_outputs": Feature(
        lambda _: {
            "include": [
                "code_interpreter_call.outputs",
                "computer_call_output.output.image_url",
                "file_search_call.results",
                "message.input_image.image_url",
            ]
        }
    ),
    "include_web_search_sources": Feature(
        lambda _: {"include": ["web_search_call.results", "web_search_call.action.sources"]}
    ),
    "context_management": Feature(
        lambda _: {"context_management": [{"type": "compaction", "compact_threshold": 200000}]}
    ),
    "moderation": Feature(lambda _: {"moderation": {"model": "omni-moderation-latest"}}),
    "conversation": Feature(lambda m: {"conversation": f"conv_llmdv{m}"}, reference=True),
    "prompt_template": Feature(lambda m: {"prompt": {"id": f"pmpt_llmdv{m}"}}, reference=True),
    "item_reference": Feature(
        lambda m: {
            "input": [
                {"type": "item_reference", "id": f"msg_llmdv{m}"},
                {"role": "user", "content": "Reply with the single word: ok"},
            ]
        },
        reference=True,
    ),
    "input_image": Feature(
        lambda _: {
            "input": _input(
                [
                    {"type": "input_text", "text": "Describe the image in one word."},
                    {"type": "input_image", "image_url": _PNG, "detail": "low"},
                ]
            )
        }
    ),
    "input_file": Feature(
        lambda _: {
            "input": _input(
                [
                    {"type": "input_file", "filename": "note.txt", "file_data": _FILE},
                    {"type": "input_text", "text": QUESTION},
                ]
            )
        },
        effect="code_word",
    ),
    "web_search": _tool({"type": "web_search"}, "web_search_call"),
    "web_search_preview": _tool({"type": "web_search_preview"}, "web_search_call"),
    "file_search": _tool(
        {"type": "file_search", "vector_store_ids": ["vs_llmdv_probe"]}, "file_search_call"
    ),
    "code_interpreter": _tool(
        {"type": "code_interpreter", "container": {"type": "auto"}}, "code_interpreter_call"
    ),
    "computer_use_preview": _tool(
        {
            "type": "computer_use_preview",
            "display_width": 1024,
            "display_height": 768,
            "environment": "browser",
        },
        "computer_call",
    ),
    # A reserved top-level domain, so a fleet that does connect reaches nothing.
    "mcp": _tool(
        {
            "type": "mcp",
            "server_label": "llmdv_probe",
            "server_url": "https://mcp.example.invalid/sse",
            "require_approval": "never",
        },
        "mcp_call",
        "mcp_list_tools",
        "mcp_approval_request",
    ),
    "image_generation": _tool({"type": "image_generation"}, "image_generation_call"),
    "local_shell": _tool({"type": "local_shell"}, "local_shell_call"),
    "shell": _tool({"type": "shell"}, "shell_call"),
    "apply_patch": _tool({"type": "apply_patch"}, "apply_patch_call"),
    "reasoning_generate_summary": Feature(lambda _: {"reasoning": {"generate_summary": "auto"}}),
    "reasoning_mode": Feature(lambda _: {"reasoning": {"mode": "standard"}}),
    "tool_search": _tool({"type": "tool_search"}, "tool_search_call"),
    "programmatic_tool_calling": _tool({"type": "programmatic_tool_calling"}, "program"),
    "namespace_tools": _tool(
        {
            "type": "namespace",
            "name": "llmdv_tools",
            "description": "Tools grouped under one namespace.",
            "tools": [
                {
                    "type": "function",
                    "name": "llmdv_lookup",
                    "description": "Looks up a value.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
    ),
    "stream_obfuscation_off": Feature(
        lambda _: {"stream": True, "stream_options": {"include_obfuscation": False}},
        effect="no_obfuscation",
        stream=True,
    ),
    "access_programs": Feature(lambda _: {"access_programs": {"cyber": "standard"}}),
    "additional_tools": Feature(
        lambda _: {
            "input": [
                {"type": "additional_tools", "role": "developer", "tools": [_LOOKUP]},
                {"role": "user", "content": "Reply with the single word: ok"},
            ]
        }
    ),
}
