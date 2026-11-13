"""POST /v1/messages -- Anthropic content blocks and stop reasons.

Text and thinking events normalise to the same completion as a non-streaming response.
Streaming tool-argument reconstruction and tool loops remain part of issue #27.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from ..http.sse import Transcript
from ..profile.schema import Model
from .base import (
    DEFAULT_PROMPT,
    REASONING_PROMPT,
    TOOL_NAME,
    Chunk,
    Completion,
    ErrorEnvelope,
    Surface,
    ToolCall,
    ToolSpec,
    Usage,
    register,
)
from .cache import CacheUsage, messages_cache, stream_frames
from .conversation import Conversation, messages_history, messages_system
from .openai import reasoning_from


@dataclass(frozen=True)
class MessageChunk(Chunk):
    block_index: int | None = None
    block_start: dict[str, Any] | None = None
    input_json_delta: str | None = None
    signature_delta: str = ""


def tool_calls_from(blocks: list[dict[str, Any]]) -> tuple[ToolCall, ...]:
    return tuple(
        ToolCall(
            block.get("id"),
            block.get("name"),
            block.get("input") if isinstance(block.get("input"), dict) else None,
            index,
            block,
        )
        for index, block in enumerate(blocks)
        if isinstance(block, dict) and block.get("type") == "tool_use"
    )


def assemble_blocks(chunks: list[Chunk]) -> list[dict[str, Any]]:
    blocks, arguments = {}, {}
    for chunk in chunks:
        if not isinstance(chunk, MessageChunk) or chunk.block_index is None:
            continue
        index = chunk.block_index
        if chunk.block_start is not None:
            blocks[index] = dict(chunk.block_start)
        else:
            block = blocks.setdefault(index, {})
            for key, text in (
                ("text", chunk.content_delta),
                ("thinking", chunk.reasoning_delta),
                ("signature", chunk.signature_delta),
            ):
                if text:
                    block[key] = block.get(key, "") + text
            if chunk.input_json_delta is not None:
                arguments.setdefault(index, []).append(chunk.input_json_delta)
    for index, parts in arguments.items():
        try:
            value = json.loads("".join(parts))
        except ValueError:
            value = None
        blocks[index]["input"] = value if isinstance(value, dict) else None
    return [blocks[i] for i in sorted(blocks)]


def _mapping(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _carries_cache(body: dict[str, Any]) -> bool:
    usage = _mapping(body.get("usage"))
    return any(
        usage.get(field) is not None
        for field in ("cache_read_input_tokens", "cache_creation_input_tokens")
    )


def _text(part: dict[str, Any], field: str) -> str:
    value = part.get(field)
    if part.get("type") in (field, f"{field}_delta") and isinstance(value, str):
        return value
    return ""


def _usage(raw: Any) -> Usage:
    usage = _mapping(raw)
    # Anthropic reports no total_tokens; cache details remain in raw.
    return Usage(
        prompt_tokens=usage.get("input_tokens"), completion_tokens=usage.get("output_tokens")
    )


# The beta value that pairs with the `context_management` body field.
CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"


class Messages(Surface):
    name = "messages"
    path = "/v1/messages"
    object_type = "message"
    stream_object_type = "message"
    role = "assistant"
    count_tokens_path = "/v1/messages/count_tokens"
    requires_capability = None
    features = frozenset(
        {
            "cache_usage",
            "image_input",
            "structured_output",
            "tool_loop",
            "tools",
        }
    )

    stream_stop = "message_stop"

    def tool_finish_reason(self, model: Model) -> str:
        # A forced tool_use block ends the turn with tool_use; there is nothing to declare.
        return "tool_use"

    def assert_stream(self, transcript: Transcript) -> None:
        from ..assertions.messages import assert_messages_stream

        assert_messages_stream(transcript)

    def parse_cache_usage(self, body: dict[str, Any]) -> CacheUsage:
        return messages_cache(body)

    def stream_cache_usage(self, transcript: Transcript) -> CacheUsage:
        # message_start is authoritative when it carries the cache counters, even a cold 0
        # the final delta contradicts. When it omits them, a client still sees them: the SDK
        # copies a message_delta's cumulative input and cache counts into the final message.
        # The last delta carrying them supplies input_tokens and both cache fields as one
        # set, so a start count is never summed with delta counts.
        frames = stream_frames(transcript)
        start = _mapping(
            next((f for f in frames if f.get("type") == "message_start"), {}).get("message")
        )
        if _carries_cache(start):
            return messages_cache(start)
        deltas = [_mapping(f) for f in frames if f.get("type") == "message_delta"]
        carrying = [delta for delta in deltas if _carries_cache(delta)]
        return messages_cache(carrying[-1] if carrying else start)

    def minimal_body(
        self, model: str, max_tokens: int = 16, prompt: str = DEFAULT_PROMPT, **extra: Any
    ) -> dict[str, Any]:
        body = self.count_tokens_body(model, prompt)
        body.update(max_tokens=max_tokens, temperature=0)
        body.update(extra)
        return body

    def structured_output_body(
        self, model: str, schema: dict[str, Any], max_tokens: int, prompt: str
    ) -> dict[str, Any]:
        return self.minimal_body(
            model,
            max_tokens,
            prompt,
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )

    def image_body(self, model: str, max_tokens: int, prompt: str, *pngs: bytes) -> dict[str, Any]:
        sources = [
            {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(png).decode("ascii"),
            }
            for png in pngs
        ]
        return self._image_turn(model, max_tokens, prompt, sources)

    def image_url_body(self, model: str, max_tokens: int, prompt: str, url: str) -> dict[str, Any]:
        return self._image_turn(model, max_tokens, prompt, [{"type": "url", "url": url}])

    def _image_turn(self, model: str, max_tokens: int, prompt: str, sources: list[dict[str, Any]]):
        body = self.minimal_body(model, max_tokens, prompt)
        body["messages"][0]["content"] = [
            *({"type": "image", "source": source} for source in sources),
            {"type": "text", "text": prompt},
        ]
        return body

    def thinking_body(
        self,
        model: str,
        mode: str | None = None,
        effort: str | None = None,
        # Both halves of this signature answer the same review comment -- that the surface
        # should not hard-code the text it sends -- and they answer it in ways that compose.
        # REASONING_PROMPT keeps one prompt shared with the Chat reasoning cases, and is the
        # default because `assert_answer_only(..., REASONING_ANSWER)` depends on it. `prompt`
        # exists for the case that needs a different one: the effort comparison sends a longer
        # task so that low and high efforts do measurably different work, and asserts on token
        # counts rather than on the answer.
        prompt: str = REASONING_PROMPT,
        budget_tokens: int = 1024,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        # Manual thinking requires budget_tokens >= 1024 and max_tokens above that budget.
        body = self.minimal_body(model, max_tokens=max_tokens, prompt=prompt)
        # The native API does not allow a caller-selected temperature with thinking enabled.
        body.pop("temperature")
        if mode is not None:
            body["thinking"] = {"type": mode}
            if mode == "enabled":
                body["thinking"]["budget_tokens"] = budget_tokens
        if effort is not None:
            body["output_config"] = {"effort": effort}
        return body

    def system_blocks_body(self, model: str, blocks: list[str], max_tokens: int) -> dict[str, Any]:
        """A block-form `system`, cache marker on its last block, as Claude Code sends it."""
        system: list[dict[str, Any]] = [{"type": "text", "text": text} for text in blocks]
        system[-1]["cache_control"] = {"type": "ephemeral"}
        return self.minimal_body(model, max_tokens, system=system)

    def context_management_body(self, model: str, max_tokens: int) -> dict[str, Any]:
        return self.minimal_body(
            model,
            max_tokens,
            context_management={"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        )

    def parallel_tool_body(
        self, model: str, schemas: dict[str, dict[str, Any]], max_tokens: int, prompt: str
    ) -> dict[str, Any]:
        """Several independent tools offered together, with a call required."""
        body = self.minimal_body(
            model,
            max_tokens,
            prompt,
            tools=[
                {
                    "name": name,
                    "description": "Return the requested marker.",
                    "input_schema": schema,
                }
                for name, schema in schemas.items()
            ],
            tool_choice={"type": "any"},
        )
        body.pop("temperature")
        return body

    def tool_results_body(
        self, body: dict[str, Any], completion: Completion, results: dict[str, str]
    ) -> dict[str, Any]:
        """Every call answered in one user turn, keyed by the returned tool-call ID."""
        return {
            **body,
            "tool_choice": {"type": "none"},
            "messages": [
                *body["messages"],
                {"role": "assistant", "content": completion.raw["content"]},
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": call_id, "content": result}
                        for call_id, result in results.items()
                    ],
                },
            ],
        }

    def mid_system_body(
        self,
        model: str,
        max_tokens: int,
        *,
        opening: str,
        reply: str,
        question: str,
        note: str | None,
    ) -> dict[str, Any]:
        """One finished exchange and a new question, optionally with a system entry between.

        Claude Code delivers skill lists and reminders this way rather than in the top-level
        `system` field, so a layer that drops the entry removes them without any error.
        """
        body = self.minimal_body(model, max_tokens, opening)
        body["messages"].append({"role": "assistant", "content": reply})
        if note is not None:
            body["messages"].append({"role": "system", "content": note})
        body["messages"].append({"role": "user", "content": question})
        return body

    def continuation_body(
        self, body: dict[str, Any], blocks: list[dict[str, Any]], prompt: str
    ) -> dict[str, Any]:
        """The same request extended by the assistant's own content blocks and a new user turn."""
        return {
            **body,
            "messages": [
                *body["messages"],
                {"role": "assistant", "content": blocks},
                {"role": "user", "content": prompt},
            ],
        }

    def count_tokens_body(
        self, model: str, prompt: str = DEFAULT_PROMPT, **extra: Any
    ) -> dict[str, Any]:
        """The Messages input without generation-only fields such as max_tokens."""
        body: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        body.update(extra)
        return body

    def tools_body(
        self, model: str, max_tokens: int, prompt: str, tools: list[ToolSpec], **extra: Any
    ) -> dict[str, Any]:
        definitions = [
            {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}
            for tool in tools
        ]
        return self.minimal_body(model, max_tokens, prompt, tools=definitions, **extra)

    def tool_body(self, model: str, schema: dict[str, Any], max_tokens: int) -> dict[str, Any]:
        body = self.tools_body(
            model,
            max_tokens,
            f"Call {TOOL_NAME} once. After its result, reply with only the result value.",
            [ToolSpec(TOOL_NAME, "Return the requested marker.", schema)],
            tool_choice={"type": "tool", "name": TOOL_NAME, "disable_parallel_tool_use": True},
        )
        body.pop("temperature")
        return body

    def thinking_tool_body(
        self, model: str, schema: dict[str, Any], mode: str, max_tokens: int
    ) -> dict[str, Any]:
        """A tool request with thinking on. The call is not forced, which thinking forbids."""
        body = self.tool_body(model, schema, max(max_tokens, 2048))
        body["tool_choice"] = {"type": "auto"}
        body["thinking"] = {"type": mode}
        if mode == "enabled":
            body["thinking"]["budget_tokens"] = 1024
        return body

    def tool_result_body(
        self, body: dict[str, Any], completion: Completion, result: str
    ) -> dict[str, Any]:
        return {
            **body,
            "tool_choice": {"type": "none"},
            "messages": [
                *body["messages"],
                {"role": "assistant", "content": completion.raw["content"]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": completion.tool_calls[0].id,
                            "content": result,
                        }
                    ],
                },
            ],
        }

    def invalid_body(self, model: str, error_class: str, stream: bool = False) -> dict[str, Any]:
        body = self.stream_body(model, max_tokens=8) if stream else self.minimal_body(model, 8)
        return self._invalidate(body, error_class)

    def invalid_count_tokens_body(self, model: str, error_class: str) -> dict[str, Any]:
        if error_class not in ("missing_required_field", "wrong_field_type"):
            raise ValueError(f"count_tokens has no invalid body for {error_class!r}")
        return self._invalidate(self.count_tokens_body(model), error_class)

    def _invalidate(self, body: dict[str, Any], error_class: str) -> dict[str, Any]:
        if error_class == "missing_required_field":
            del body["messages"]
        elif error_class == "wrong_field_type":
            body["messages"] = "not a list"
        elif error_class == "invalid_token_limit":
            body["max_tokens"] = -5
        elif error_class == "invalid_tool_schema":
            body["tools"] = [{"name": TOOL_NAME}]  # missing input_schema
        elif error_class == "invalid_tool_choice":
            body["tool_choice"] = {"type": "tool", "name": "llmdv-not-a-tool"}
        else:
            raise ValueError(f"{self.name} has no invalid body for {error_class!r}")
        return body

    def extra_headers(self) -> dict[str, str]:
        return {"anthropic-version": "2023-06-01"}

    def parse_completion(self, body: dict[str, Any]) -> Completion:
        blocks = body.get("content")
        if not isinstance(blocks, list):
            blocks = []
        text_parts, thinking_parts = [], []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif block.get("type") == "thinking":
                thinking = block.get("thinking")
                if isinstance(thinking, str):
                    thinking_parts.append(thinking)

        content, reasoning = "".join(text_parts), "".join(thinking_parts)
        source = carrier = None
        if not reasoning:
            content, reasoning, source, carrier = reasoning_from(content, {})
        return Completion(
            tool_calls=tool_calls_from(blocks),
            role=body.get("role"),
            text=content,
            reasoning_text=reasoning,
            reasoning_source=source,
            reasoning_carrier=carrier,
            finish_reason=body.get("stop_reason"),
            object_type=body.get("type"),
            model_echo=body.get("model"),
            usage=_usage(body.get("usage")),
            raw=body,
        )

    def parse_error(self, body: Any) -> ErrorEnvelope | None:
        if not isinstance(body, dict) or body.get("type") != "error":
            return None
        error = body.get("error")
        if not isinstance(error, dict):
            return None
        return ErrorEnvelope(type=error.get("type"), message=error.get("message"), raw=body)

    def looks_like_success(self, body: Any) -> bool:
        return isinstance(body, dict) and body.get("type") == "message"

    def stream_body(
        self, model: str, max_tokens: int = 16, include_usage: bool = False, **extra: Any
    ) -> dict[str, Any]:
        # Messages always reports native stream usage; no opt-in field is needed.
        return {**self.minimal_body(model, max_tokens=max_tokens, **extra), "stream": True}

    def parse_chunk(self, body: dict[str, Any]) -> Chunk:
        kind = body.get("type")
        if kind == "message_start":
            message = _mapping(body.get("message"))
            usage = _mapping(message.get("usage"))
            # The start's output count is provisional; only message_delta's is final.
            return Chunk(
                role=message.get("role"),
                usage=Usage(prompt_tokens=usage.get("input_tokens")),
                raw=body,
            )
        if kind in ("content_block_start", "content_block_delta"):
            part = _mapping(body.get("content_block" if kind == "content_block_start" else "delta"))
            return MessageChunk(
                block_index=body.get("index") if type(body.get("index")) is int else None,
                block_start=part if kind == "content_block_start" else None,
                input_json_delta=part.get("partial_json")
                if part.get("type") == "input_json_delta"
                else None,
                signature_delta=_text(part, "signature"),
                content_delta=_text(part, "text"),
                reasoning_delta=_text(part, "thinking"),
                raw=body,
            )
        if kind == "message_delta":
            return Chunk(
                finish_reason=_mapping(body.get("delta")).get("stop_reason"),
                usage=_usage(body.get("usage")),
                raw=body,
            )
        return Chunk(raw=body)

    def assemble(self, chunks: list[Chunk]) -> Completion:
        start = next((c.raw for c in chunks if c.raw.get("type") == "message_start"), {})
        message = start.get("message")
        message = message if isinstance(message, dict) else {}
        content = "".join(c.content_delta for c in chunks)
        reasoning = "".join(c.reasoning_delta for c in chunks)
        source = carrier = None
        if not reasoning:
            content, reasoning, source, carrier = reasoning_from(content, {})
        prompt_tokens = completion_tokens = None
        for chunk in chunks:
            if chunk.usage is not None:
                if chunk.usage.prompt_tokens is not None:
                    prompt_tokens = chunk.usage.prompt_tokens
                if chunk.usage.completion_tokens is not None:
                    completion_tokens = chunk.usage.completion_tokens
        blocks = assemble_blocks(chunks)
        return Completion(
            streamed=True,
            tool_calls=tool_calls_from(blocks),
            role=message.get("role"),
            object_type=message.get("type"),
            model_echo=message.get("model"),
            text=content,
            reasoning_text=reasoning,
            reasoning_source=source,
            reasoning_carrier=carrier,
            finish_reason=next(
                (c.finish_reason for c in reversed(chunks) if c.finish_reason), None
            ),
            usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
            raw={"events": [c.raw for c in chunks], "content": blocks},
        )

    def conversation_body(
        self, model: str, conversation: Conversation, max_tokens: int, stream: bool = False
    ) -> dict[str, Any]:
        """A session's next request: block system, history and offered tools."""
        body = (
            self.stream_body(model, max_tokens) if stream else self.minimal_body(model, max_tokens)
        )
        body["messages"] = messages_history(conversation)
        if conversation.system:
            body["system"] = messages_system(conversation)
        if conversation.tools:
            body["tools"] = self.tools_body(model, max_tokens, "", list(conversation.tools))[
                "tools"
            ]
        return body


MESSAGES = register(Messages())
