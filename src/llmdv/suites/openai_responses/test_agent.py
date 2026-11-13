"""The first request of a session from a Responses-based coding agent, as a generic fixture.

Such a client keeps no server state: it sends its system prompt as `instructions`, policy as
a developer item, environment and task as user items, its function tools, a reasoning
summary request, encrypted reasoning for stateless replay, a cache key, and streams.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ...assertions import assert_answer_only
from ...surfaces.base import DEFAULT_ANSWER, DEFAULT_PROMPT
from ...surfaces.responses import TERMINAL_EVENTS
from ..conftest import budget_for, check_completion, check_sse_response

pytestmark = [pytest.mark.surface("responses"), pytest.mark.needs("tools")]

INSTRUCTIONS = (
    "You are a coding agent running in a terminal on the user's machine. Keep answers short. "
    "Use the shell tool to inspect files and apply_patch to edit them. Never invent output."
)


def _text(role: str, text: str) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def _function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS = [
    _function(
        "shell",
        "Runs a command and returns its output.",
        {
            "command": {"type": "array", "items": {"type": "string"}},
            "workdir": {"type": "string"},
            "timeout_ms": {"type": "number"},
        },
        ["command"],
    ),
    _function(
        "apply_patch",
        "Applies a patch in the agent's patch format.",
        {"input": {"type": "string"}},
        ["input"],
    ),
    _function(
        "update_plan",
        "Records the steps of the current plan and their status.",
        {
            "plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"step": {"type": "string"}, "status": {"type": "string"}},
                    "required": ["step", "status"],
                    "additionalProperties": False,
                },
            }
        },
        ["plan"],
    ),
]


def agent_request(model: str, max_tokens: int) -> dict[str, Any]:
    return {
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": [
            _text("developer", "Commands run in a sandbox. Ask before writing outside /work."),
            _text("user", "<environment_context>cwd: /work, shell: bash</environment_context>"),
            _text("user", f"{DEFAULT_PROMPT}. Do not call any tool."),
        ],
        "tools": TOOLS,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "medium", "summary": "auto"},
        "text": {"verbosity": "medium"},
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": str(uuid.uuid4()),
        "store": False,
        "stream": True,
        "max_output_tokens": max_tokens,
    }


@pytest.mark.case_id("openai_responses.agent_request")
def test_coding_agent_request_streams(client, surface, model, ingress):
    """The session's first request streams a valid lifecycle to a healthy answer."""
    body = agent_request(model.public_id, budget_for(model, floor=256))
    exchange, transcript = client.stream(
        surface.path, json_body=body, stop_event=TERMINAL_EVENTS | {"error"}
    )
    check_sse_response(exchange, ingress)
    surface.assert_stream(transcript)
    completion = surface.assemble(
        [surface.parse_chunk(event.json()) for event in transcript.data_events]
    )
    check_completion(completion, surface, model)
    assert_answer_only(completion, model, DEFAULT_ANSWER)
