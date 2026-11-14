"""POST /v1/responses -- typed output items, response status and native token usage.

Streaming deltas supply the output; the terminal snapshot supplies status and usage.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..http.sse import Transcript
from ..profile.schema import Model
from .base import (
    DEFAULT_PROMPT,
    TOOL_NAME,
    Chunk,
    Completion,
    Surface,
    ToolCall,
    ToolSpec,
    Usage,
    register,
)
from .cache import CacheUsage, openai_cache, stream_frames
from .conversation import Conversation, openai_cache_hints, responses_input
from .images import png_data_url
from .openai import OpenAIErrors, reasoning_from

FUNCTION_CALL = "function_call"
FUNCTION_ARGUMENTS = "response.function_call_arguments"

TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})
TEXT_EVENTS = {
    "response.output_text": "output_text",
    "response.reasoning_text": "reasoning_text",
    "response.reasoning_summary_text": "summary_text",
    "response.refusal": "refusal",
}
PART_EVENTS = (
    "response.content_part",
    "response.reasoning_part",
    "response.reasoning_summary_part",
)


@dataclass(frozen=True)
class ResponseChunk(Chunk):
    """Responses can stream both full reasoning and a summary for the same output item."""

    output_index: int = 0
    part_index: int = 0
    summary_delta: str = ""
    tool_item: dict[str, Any] | None = None
    tool_arguments_delta: str = ""


def _join_deltas(chunks: list[ResponseChunk], field: str) -> str:
    parts: dict[tuple[int, int], list[str]] = {}
    for chunk in chunks:
        parts.setdefault((chunk.output_index, chunk.part_index), []).append(getattr(chunk, field))
    return "".join("".join(parts[index]) for index in sorted(parts))


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _text(parts: Any, kind: str) -> str:
    return "".join(
        part["text"]
        for part in _items(parts)
        if part.get("type") == kind and isinstance(part.get("text"), str)
    )


def tool_calls_from(items: list[dict[str, Any]]) -> tuple[ToolCall, ...]:
    return tuple(
        ToolCall(
            item.get("call_id"),
            item.get("name"),
            item.get("arguments") if isinstance(item.get("arguments"), str) else None,
            index,
            item,
        )
        for index, item in enumerate(_items(items))
        if item.get("type") == FUNCTION_CALL
    )


def streamed_tools(chunks: list[ResponseChunk]) -> tuple[ToolCall, ...]:
    items = {}
    for chunk in chunks:
        if chunk.tool_item is not None:
            items[chunk.output_index] = dict(chunk.tool_item)
        if chunk.tool_arguments_delta and chunk.output_index in items:
            item = items[chunk.output_index]
            item["arguments"] = (item.get("arguments") or "") + chunk.tool_arguments_delta
    return tuple(
        ToolCall(item.get("call_id"), item.get("name"), item.get("arguments"), index, item)
        for index, item in sorted(items.items())
    )


class Responses(OpenAIErrors, Surface):
    name = "responses"
    path = "/v1/responses"
    object_type = "response"
    stream_object_type = "response"
    role = None
    features = frozenset(
        {
            "cache_usage",
            "image_input",
            "json_object",
            "reasoning_usage",
            "structured_output",
            "tool_loop",
            "tools",
            "vllm_options",
        }
    )
    observable_vllm_options = frozenset(("ignore_eos",))

    stream_stop = TERMINAL_EVENTS | {"error"}

    def tool_finish_reason(self, model: Model) -> str:
        # Responses reports a status, not a tool finish: a forced call completes normally.
        return "stop"

    def native_contract(self, raw: dict[str, Any]) -> None:
        from ..assertions.responses import assert_response_contract

        assert_response_contract(raw)

    def assert_stream(self, transcript: Transcript) -> None:
        from ..assertions.responses import assert_response_stream_output, assert_responses_stream

        assert_responses_stream(transcript)
        completion = self.assemble([self.parse_chunk(e.json()) for e in transcript.data_events])
        assert_response_stream_output(completion, self.parse_completion(completion.raw))

    def parse_cache_usage(self, body: dict[str, Any]) -> CacheUsage:
        return openai_cache(body, "input_tokens", "input_tokens_details")

    def stream_cache_usage(self, transcript: Transcript) -> CacheUsage:
        # Usage arrives only in the terminal snapshot.
        frames = stream_frames(transcript)
        end = next((f for f in reversed(frames) if f.get("type") in TERMINAL_EVENTS), {})
        return self.parse_cache_usage(_mapping(end.get("response")))

    def minimal_body(
        self, model: str, max_tokens: int = 16, prompt: str = DEFAULT_PROMPT, **extra: Any
    ) -> dict[str, Any]:
        body = {
            "model": model,
            "input": prompt,
            "max_output_tokens": max_tokens,
            "temperature": 0,
            "store": False,
        }
        body.update(extra)
        return body

    def structured_output_body(
        self, model: str, schema: dict[str, Any], max_tokens: int, prompt: str
    ) -> dict[str, Any]:
        return self.minimal_body(
            model,
            max_tokens,
            prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "llmdv_probe",
                    "strict": True,
                    "schema": schema,
                }
            },
        )

    def image_body(self, model: str, max_tokens: int, prompt: str, *pngs: bytes) -> dict[str, Any]:
        return self._image_turn(model, max_tokens, prompt, [png_data_url(png) for png in pngs])

    def image_url_body(self, model: str, max_tokens: int, prompt: str, url: str) -> dict[str, Any]:
        return self._image_turn(model, max_tokens, prompt, [url])

    def _image_turn(self, model: str, max_tokens: int, prompt: str, urls: list[str]):
        body = self.minimal_body(model, max_tokens, prompt)
        content = [
            # `detail` is required of an input image, and engines validate it.
            *({"type": "input_image", "image_url": url, "detail": "auto"} for url in urls),
            {"type": "input_text", "text": prompt},
        ]
        body["input"] = [{"role": "user", "content": content}]
        return body

    def option_body(self, model: str, max_tokens: int, option: str, value: Any) -> dict[str, Any]:
        if option == "metadata":
            extra = {"metadata": value}
        elif option == "logprobs":
            extra = {"include": ["message.output_text.logprobs"], "top_logprobs": value}
        elif option == "reasoning":
            extra = {"reasoning": {"effort": value}}
        else:
            raise ValueError(f"unknown Responses option {option!r}")
        return self.minimal_body(model, max_tokens, **extra)

    def tools_body(
        self, model: str, max_tokens: int, prompt: str, tools: list[ToolSpec], **extra: Any
    ) -> dict[str, Any]:
        definitions = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in tools
        ]
        return self.minimal_body(model, max_tokens, prompt, tools=definitions, **extra)

    def tool_body(self, model: str, schema: dict[str, Any], max_tokens: int) -> dict[str, Any]:
        return self.tools_body(
            model,
            max_tokens,
            f"Call {TOOL_NAME} once. After its result, reply with only the result value.",
            [ToolSpec(TOOL_NAME, "Return the requested marker.", schema)],
            tool_choice={"type": "function", "name": TOOL_NAME},
            parallel_tool_calls=False,
        )

    def tool_result_body(
        self, body: dict[str, Any], completion: Completion, result: str
    ) -> dict[str, Any]:
        return {
            **body,
            "tool_choice": "none",
            "input": [
                {"role": "user", "content": body["input"]},
                *completion.raw["output"],
                {
                    "type": "function_call_output",
                    "call_id": completion.tool_calls[0].id,
                    "output": result,
                },
            ],
        }

    def json_object_body(self, model: str, max_tokens: int) -> dict[str, Any]:
        return self.minimal_body(
            model,
            max_tokens,
            "Reply with the plain text ok, without a JSON wrapper.",
            text={"format": {"type": "json_object"}},
        )

    def invalid_body(self, model: str, error_class: str, stream: bool = False) -> dict[str, Any]:
        body = self.stream_body(model, max_tokens=8) if stream else self.minimal_body(model, 8)
        if error_class == "missing_required_field":
            del body["input"]
        elif error_class == "wrong_field_type":
            body["input"] = {"not": "a string or input item array"}
        elif error_class == "invalid_token_limit":
            body["max_output_tokens"] = -5
        elif error_class == "invalid_tool_schema":
            body["tools"] = [{"type": "function"}]  # missing the flat function name
        elif error_class == "invalid_tool_choice":
            body["tool_choice"] = {"type": "function", "name": "llmdv-not-a-tool"}
        else:
            raise ValueError(f"{self.name} has no invalid body for {error_class!r}")
        return body

    def parse_completion(self, body: dict[str, Any]) -> Completion:
        output = _items(body.get("output"))
        messages = [item for item in output if item.get("type") == "message"]
        text = "".join(_text(item.get("content"), "output_text") for item in messages)
        reasoning = "".join(
            _text(item.get("content"), "reasoning_text")
            or _text(item.get("summary"), "summary_text")
            for item in output
            if item.get("type") == "reasoning"
        )
        source = carrier = None
        if not reasoning:
            text, reasoning, source, carrier = reasoning_from(text, {})
        status = body.get("status")
        # Keep native diagnostics without letting a made-up status like "stop" pass.
        finish = f"status: {status}"
        if body.get("error") is None:
            if status == "completed" and body.get("incomplete_details") is None:
                finish = "stop"
            elif status == "incomplete":
                reason = _mapping(body.get("incomplete_details")).get("reason")
                if reason == "max_output_tokens":
                    finish = "length"
                elif reason == "content_filter":
                    finish = "content_filter"
            elif status == "cancelled":
                finish = "cancelled"
        else:
            finish = f"error: {_mapping(body.get('error')).get('code')}"
        usage = _mapping(body.get("usage"))
        return Completion(
            tool_calls=tool_calls_from(output),
            role=messages[0].get("role") if messages else None,
            object_type=body.get("object"),
            model_echo=body.get("model"),
            text=text,
            reasoning_text=reasoning,
            reasoning_source=source,
            reasoning_carrier=carrier,
            finish_reason=finish,
            usage=Usage(
                reasoning_tokens=_mapping(usage.get("output_tokens_details")).get(
                    "reasoning_tokens"
                ),
                prompt_tokens=usage.get("input_tokens"),
                completion_tokens=usage.get("output_tokens"),
                total_tokens=usage.get("total_tokens"),
            ),
            raw=body,
        )

    def looks_like_success(self, body: Any) -> bool:
        return isinstance(body, dict) and body.get("object") == "response"

    def stream_body(
        self, model: str, max_tokens: int = 16, include_usage: bool = False, **extra: Any
    ) -> dict[str, Any]:
        # Responses always reports native stream usage; no opt-in field is needed.
        return {**self.minimal_body(model, max_tokens=max_tokens, **extra), "stream": True}

    def parse_chunk(self, body: dict[str, Any]) -> ResponseChunk:
        kind = body.get("type")
        if not isinstance(kind, str):
            return ResponseChunk(raw=body)
        if kind == "response.output_item.added":
            item = _mapping(body.get("item"))
            if item.get("type") == FUNCTION_CALL:
                return ResponseChunk(
                    output_index=body.get("output_index", 0), tool_item=item, raw=body
                )
        if kind == f"{FUNCTION_ARGUMENTS}.delta":
            delta = body.get("delta")
            return ResponseChunk(
                output_index=body.get("output_index", 0),
                tool_arguments_delta=delta if isinstance(delta, str) else "",
                raw=body,
            )
        prefix = kind.removesuffix(".delta")
        if kind.endswith(".delta") and prefix in TEXT_EVENTS:
            text, part_kind = body.get("delta"), TEXT_EVENTS[prefix]
        elif kind.endswith(".added") and kind.removesuffix(".added") in PART_EVENTS:
            part = _mapping(body.get("part"))
            text, part_kind = part.get("text"), part.get("type")
        else:
            return ResponseChunk(raw=body)
        text = text if isinstance(text, str) else ""
        return ResponseChunk(
            output_index=body.get("output_index", 0),
            part_index=body.get("summary_index", body.get("content_index", 0)),
            content_delta=text if part_kind == "output_text" else "",
            reasoning_delta=text if part_kind == "reasoning_text" else "",
            summary_delta=text if part_kind == "summary_text" else "",
            raw=body,
        )

    def assemble(self, chunks: list[ResponseChunk]) -> Completion:
        end = next((c.raw for c in reversed(chunks) if c.raw.get("type") in TERMINAL_EVENTS), {})
        terminal = self.parse_completion(_mapping(end.get("response")))
        # Match non-streaming normalization: prefer full reasoning over its summary per item.
        items: dict[int, list[ResponseChunk]] = {}
        for chunk in chunks:
            items.setdefault(chunk.output_index, []).append(chunk)
        text = _join_deltas(chunks, "content_delta")
        thinking = "".join(
            _join_deltas(items[index], "reasoning_delta")
            or _join_deltas(items[index], "summary_delta")
            for index in sorted(items)
        )
        source = carrier = None
        if not thinking:
            text, thinking, source, carrier = reasoning_from(text, {})
        return replace(
            terminal,
            streamed=True,
            text=text,
            reasoning_text=thinking,
            reasoning_source=source,
            reasoning_carrier=carrier,
            tool_calls=streamed_tools(chunks),
        )

    def conversation_body(
        self, model: str, conversation: Conversation, max_tokens: int, stream: bool = False
    ) -> dict[str, Any]:
        """A stateless session's next request: instructions, input items and offered tools."""
        body = (
            self.stream_body(model, max_tokens) if stream else self.minimal_body(model, max_tokens)
        )
        body["input"] = responses_input(conversation)
        body.update(openai_cache_hints(conversation))
        if conversation.system:
            body["instructions"] = "\n\n".join(conversation.system)
        if conversation.tools:
            body["tools"] = self.tools_body(model, max_tokens, "", list(conversation.tools))[
                "tools"
            ]
        return body


RESPONSES = register(Responses())
