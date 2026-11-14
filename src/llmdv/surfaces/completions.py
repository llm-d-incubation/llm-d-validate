"""POST /v1/completions -- prompt input and text choices.

OpenAI errors, streaming request options and chunk assembly are shared with chat. Requests
and response choices have their own grammar: no messages, roles, deltas or tools.
"""

from __future__ import annotations

from typing import Any

from .base import DEFAULT_PROMPT, OPEN_PROMPT, Chunk, Completion, register
from .openai import OpenAIChoicesSurface, split_inline_thinking, usage_from


class Completions(OpenAIChoicesSurface):
    name = "completions"
    path = "/v1/completions"
    object_type = "text_completion"
    stream_object_type = "text_completion"
    role = None
    features = frozenset()
    # No chat template: a raw instruction may end at once, an open sentence is continued.
    reply_prompt = OPEN_PROMPT

    def minimal_body(
        self, model: str, max_tokens: int = 16, prompt: str = DEFAULT_PROMPT, **extra: Any
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        body.update(extra)
        return body

    def invalid_body(self, model: str, error_class: str, stream: bool = False) -> dict[str, Any]:
        body = self.stream_body(model, max_tokens=8) if stream else self.minimal_body(model, 8)
        if error_class == "missing_required_field":
            del body["prompt"]
        elif error_class == "wrong_field_type":
            # Strings, token IDs and batches are all valid prompts; an object is not.
            body["prompt"] = {"not": "a prompt"}
        elif error_class == "invalid_token_limit":
            body["max_tokens"] = -5
        else:
            raise ValueError(f"{self.name} has no invalid body for {error_class!r}")
        return body

    def parse_completion(self, body: dict[str, Any]) -> Completion:
        choices = body.get("choices") or []
        choice = choices[0] if choices else {}
        content, reasoning = split_inline_thinking(choice.get("text") or "")
        return Completion(
            role=None,
            text=content,
            reasoning_text=reasoning,
            finish_reason=choice.get("finish_reason"),
            object_type=body.get("object"),
            model_echo=body.get("model"),
            usage=usage_from(body.get("usage")),
            raw=body,
        )

    def parse_chunk(self, body: dict[str, Any]) -> Chunk:
        # Unlike chat, streamed completions also put each text fragment in choices[].text.
        choices = body.get("choices") or []
        choice = choices[0] if choices else {}
        raw_usage = body.get("usage")
        return Chunk(
            content_delta=choice.get("text") or "",
            finish_reason=choice.get("finish_reason"),
            usage=usage_from(raw_usage) if raw_usage else None,
            has_choices=bool(choices),
            raw=body,
        )

    def assert_stream(self, transcript) -> None:
        from ..assertions.frames import assert_completion_frames

        assert_completion_frames(transcript)


COMPLETIONS = register(Completions())
