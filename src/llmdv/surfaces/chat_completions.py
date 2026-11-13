"""POST /v1/chat/completions -- the OpenAI chat surface.

Error envelope on this surface is OpenAI-shaped:

    {"error": {"message": ..., "type": ..., "param": ..., "code": ...}}

verified live against vLLM v0.30.0 and the llm-d router gateway, both of which return 404
with exactly that body for an unknown model.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..http.sse import Transcript
from ..profile.schema import Model
from .base import (
    DEFAULT_PROMPT,
    REASONING_PROMPT,
    TOOL_NAME,
    Chunk,
    Completion,
    ToolCall,
    ToolSpec,
    register,
)
from .cache import CacheUsage, openai_cache, stream_frames
from .conversation import Conversation, chat_messages, openai_cache_hints
from .images import png_data_url
from .openai import OpenAIChoicesSurface, reasoning_from, usage_from


@dataclass(frozen=True)
class ChatChunk(Chunk):
    tool_fragments: tuple[dict[str, Any], ...] = ()
    reasoning_fields: dict[str, str] = field(default_factory=dict)


class ChatCompletions(OpenAIChoicesSurface):
    name = "chat_completions"
    path = "/v1/chat/completions"
    object_type = "chat.completion"
    stream_object_type = "chat.completion.chunk"
    role = "assistant"
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
    observable_vllm_options = frozenset(
        ("return_token_ids", "min_tokens", "ignore_eos", "truncate_prompt_tokens")
    )

    def tool_finish_reason(self, model: Model) -> str:
        """The `finish_reason` the profile declares for a forced named function."""
        return model.tools.forced_finish_reason

    def assert_stream(self, transcript: Transcript) -> None:
        from ..assertions import fail
        from ..assertions.chat_choices import assert_choice_stream

        frames = [event.json() for event in transcript.data_events]
        if not transcript.saw_done:
            fail("terminal_done", False, "stream ended with [DONE]")
        model = frames[0].get("model") if frames and isinstance(frames[0], dict) else None
        assert_choice_stream(frames, 1, model)

    def parse_cache_usage(self, body: dict[str, Any]) -> CacheUsage:
        return openai_cache(body, "prompt_tokens", "prompt_tokens_details")

    def stream_cache_usage(self, transcript: Transcript) -> CacheUsage:
        # A streaming client reads usage from the choice-less frame include_usage adds.
        terminal = [
            frame
            for frame in stream_frames(transcript)
            if frame.get("usage") and not frame.get("choices")
        ]
        return self.parse_cache_usage(terminal[-1] if terminal else {})

    def minimal_body(
        self, model: str, max_tokens: int = 16, prompt: str = DEFAULT_PROMPT, **extra: Any
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            # Deterministic so a rerun is comparable; a sampled reply makes a diff useless.
            "temperature": 0,
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
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "llmdv_probe", "strict": True, "schema": schema},
            },
        )

    def image_body(self, model: str, max_tokens: int, prompt: str, *pngs: bytes) -> dict[str, Any]:
        return self._image_turn(model, max_tokens, prompt, [png_data_url(png) for png in pngs])

    def image_url_body(self, model: str, max_tokens: int, prompt: str, url: str) -> dict[str, Any]:
        return self._image_turn(model, max_tokens, prompt, [url])

    def _image_turn(self, model: str, max_tokens: int, prompt: str, urls: list[str]):
        body = self.minimal_body(model, max_tokens, prompt)
        body["messages"][0]["content"] = [
            *({"type": "image_url", "image_url": {"url": url}} for url in urls),
            {"type": "text", "text": prompt},
        ]
        return body

    def reasoning_body(
        self,
        model: str,
        max_tokens: int,
        effort: str | None = None,
        stream: bool = False,
        prompt: str = REASONING_PROMPT,
    ) -> dict[str, Any]:
        extra = {"reasoning_effort": effort} if effort is not None else {}
        if stream:
            return self.stream_body(model, max_tokens, include_usage=True, prompt=prompt, **extra)
        return self.minimal_body(model, max_tokens, prompt, **extra)

    def tools_body(
        self, model: str, max_tokens: int, prompt: str, tools: list[ToolSpec], **extra: Any
    ) -> dict[str, Any]:
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
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
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            parallel_tool_calls=False,
        )

    def tool_result_body(
        self, body: dict[str, Any], completion: Completion, result: str
    ) -> dict[str, Any]:
        call = completion.tool_calls[0]
        return {
            **body,
            "tool_choice": "none",
            "messages": [
                *body["messages"],
                completion.assistant_message,
                {"role": "tool", "tool_call_id": call.id, "content": result},
            ],
        }

    def tool_stream_body(
        self, model: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        return {
            **self.tool_body(model, schema, max_tokens),
            "stream": True,
            "stream_options": {"include_usage": True},
        }

    def assemble(self, chunks: list[ChatChunk]) -> Completion:
        completion = super().assemble(chunks)
        calls: dict[int, dict[str, Any]] = {}
        fields: dict[str, str] = {}
        content = "".join(chunk.content_delta for chunk in chunks)
        for chunk in chunks:
            for name, value in chunk.reasoning_fields.items():
                fields[name] = fields.get(name, "") + value
            for part in chunk.tool_fragments:
                call = calls.setdefault(
                    part["index"],
                    {"id": None, "type": None, "function": {"name": "", "arguments": ""}},
                )
                for name in ("id", "type"):
                    if part.get(name) is not None:
                        call[name] = part[name]
                for name in ("name", "arguments"):
                    call["function"][name] += (part.get("function") or {}).get(name) or ""
        native_calls = [calls[index] for index in sorted(calls)]
        text, reasoning, source, carrier = reasoning_from(content, fields)
        message = {"role": completion.role, "content": content, **fields}
        if native_calls:
            message["tool_calls"] = native_calls
        return replace(
            completion,
            text=text,
            reasoning_text=reasoning,
            reasoning_source=source,
            reasoning_carrier=carrier,
            tool_calls=tuple(
                ToolCall(c["id"], c["function"]["name"], c["function"]["arguments"], i, c)
                for i, c in zip(sorted(calls), native_calls, strict=True)
            ),
            assistant_message=message,
        )

    def json_object_body(self, model: str, max_tokens: int) -> dict[str, Any]:
        return self.minimal_body(
            model,
            max_tokens,
            "Reply with the plain text ok, without a JSON wrapper.",
            response_format={"type": "json_object"},
        )

    def invalid_body(self, model: str, error_class: str, stream: bool = False) -> dict[str, Any]:
        body = self.stream_body(model, max_tokens=8) if stream else self.minimal_body(model, 8)
        if error_class == "missing_required_field":
            del body["messages"]
        elif error_class == "wrong_field_type":
            body["messages"] = "not a list"
        elif error_class == "invalid_token_limit":
            body["max_tokens"] = -5
        elif error_class == "invalid_tool_schema":
            body["tools"] = [{"type": "function"}]  # no function definition
        elif error_class == "invalid_tool_choice":
            body["tool_choice"] = "llmdv-not-a-tool"  # names no declared tool
        else:
            raise ValueError(f"{self.name} has no invalid body for {error_class!r}")
        return body

    def parse_completion(self, body: dict[str, Any]) -> Completion:
        choices = body.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}

        content = message.get("content") or ""
        content, reasoning, source, carrier = reasoning_from(content, message)

        calls = []
        raw_calls = message.get("tool_calls")
        for index, raw in enumerate(raw_calls if isinstance(raw_calls, list) else []):
            raw = raw if isinstance(raw, dict) else {}
            function = raw.get("function")
            function = (
                function if isinstance(function, dict) and raw.get("type") == "function" else {}
            )
            arguments = function.get("arguments")
            calls.append(
                ToolCall(
                    raw.get("id"),
                    function.get("name"),
                    arguments if isinstance(arguments, str) else None,
                    index,
                    raw,
                )
            )
        return Completion(
            tool_calls=tuple(calls),
            reasoning_source=source,
            reasoning_carrier=carrier,
            assistant_message=message,
            role=message.get("role"),
            text=content,
            reasoning_text=reasoning,
            finish_reason=choice.get("finish_reason"),
            object_type=body.get("object"),
            model_echo=body.get("model"),
            usage=usage_from(body.get("usage")),
            raw=body,
        )

    def parse_completions(self, body: dict[str, Any]) -> list[Completion]:
        return [self.parse_completion({**body, "choices": [choice]}) for choice in body["choices"]]

    def assemble_choices(self, frames: list[dict[str, Any]], count: int) -> list[Completion]:
        # The terminal usage belongs to the request, not to an individual choice.
        grouped = [[] for _ in range(count)]
        for frame in frames:
            if not frame["choices"]:
                for chunks in grouped:
                    chunks.append(self.parse_chunk(frame))
            for choice in frame["choices"]:
                grouped[choice["index"]].append(self.parse_chunk({**frame, "choices": [choice]}))
        return [self.assemble(chunks) for chunks in grouped]

    # ------------------------------------------------------------------ streaming

    def parse_chunk(self, body: dict[str, Any]) -> ChatChunk:
        """Normalise one `chat.completion.chunk`.

        Unknown fields are ignored on purpose: vLLM adds `prompt_token_ids` and `token_ids`,
        and a gateway may add its own. Rejecting those would fail a working stack over fields
        no client reads.
        """
        choices = body.get("choices") or []
        choice = choices[0] if choices else {}
        delta = choice.get("delta") or {}
        raw_usage = body.get("usage")

        return ChatChunk(
            tool_fragments=tuple(delta.get("tool_calls") or []),
            reasoning_fields={
                name: delta[name]
                for name in ("reasoning", "reasoning_content")
                if isinstance(delta.get(name), str)
            },
            role=delta.get("role"),
            content_delta=delta.get("content") or "",
            reasoning_delta=delta.get("reasoning_content") or delta.get("reasoning") or "",
            finish_reason=choice.get("finish_reason"),
            usage=usage_from(raw_usage) if raw_usage else None,
            has_choices=bool(choices),
            raw=body,
        )

    def conversation_body(
        self, model: str, conversation: Conversation, max_tokens: int, stream: bool = False
    ) -> dict[str, Any]:
        """A session's next request: its system, history and offered tools."""
        body = (
            self.stream_body(model, max_tokens, include_usage=True)
            if stream
            else self.minimal_body(model, max_tokens)
        )
        body["messages"] = chat_messages(conversation)
        body.update(openai_cache_hints(conversation))
        if conversation.tools:
            body["tools"] = self.tools_body(model, max_tokens, "", list(conversation.tools))[
                "tools"
            ]
        return body


CHAT_COMPLETIONS = register(ChatCompletions())
