"""OpenAI errors shared by all surfaces, plus choices-based completion behaviour."""

from __future__ import annotations

import re
from typing import Any

from .base import Chunk, Completion, ErrorEnvelope, Surface, Usage


class OpenAIErrors:
    """The OpenAI error envelope and headers shared by chat, completions and Responses."""

    requires_capability = None

    def extra_headers(self) -> dict[str, str]:
        return {}

    def parse_error(self, body: Any) -> ErrorEnvelope | None:
        if not isinstance(body, dict):
            return None
        error = body.get("error")
        if isinstance(error, dict):
            return ErrorEnvelope(
                type=error.get("type"),
                message=error.get("message"),
                code=error.get("code"),
                param=error.get("param"),
                raw=body,
            )
        # Some layers flatten the envelope to {"message": ..., "type": ...}. Accepting that
        # keeps the assertion about "a structured error" rather than about one nesting style.
        if "message" in body and "choices" not in body:
            return ErrorEnvelope(
                type=body.get("type"),
                message=body.get("message"),
                code=body.get("code"),
                param=body.get("param"),
                raw=body,
            )
        return None


class OpenAIChoicesSurface(OpenAIErrors, Surface):
    """Streaming and success detection for chat and prompt-based choices."""

    def stream_body(
        self, model: str, max_tokens: int = 16, include_usage: bool = False, **extra: Any
    ) -> dict[str, Any]:
        body = self.minimal_body(model, max_tokens=max_tokens, **extra)
        body["stream"] = True
        if include_usage:
            body["stream_options"] = {"include_usage": True}
        return body

    def assemble(self, chunks: list[Chunk]) -> Completion:
        """Fold streamed chunks into the same Completion a non-streaming call produces.

        This is what lets the shared assertions -- not aborted, has output, usage consistent --
        run unchanged over a stream. Without it the streaming cases would grow a parallel
        assertion stack that could drift from the non-streaming one.

        Inline reasoning is split after concatenation, not per chunk: a `<think>` tag is
        routinely delivered across two frames, so splitting early would miss it.
        """
        content = "".join(c.content_delta for c in chunks)
        reasoning = "".join(c.reasoning_delta for c in chunks)
        if not reasoning:
            content, reasoning = split_inline_thinking(content)

        finish = next((c.finish_reason for c in reversed(chunks) if c.finish_reason), None)
        usage = next((c.usage for c in reversed(chunks) if c.usage), None)
        role = next((c.role for c in chunks if c.role), None)
        model_echo = next((c.raw.get("model") for c in chunks if c.raw.get("model")), None)

        return Completion(
            streamed=True,
            role=role,
            text=content,
            reasoning_text=reasoning,
            finish_reason=finish,
            object_type=next((c.raw.get("object") for c in chunks if c.raw.get("object")), None),
            model_echo=model_echo,
            usage=usage or Usage(),
            raw={"chunks": [c.raw for c in chunks]},
        )

    def looks_like_success(self, body: Any) -> bool:
        """Whether a body carries a generation.

        Used by the no-silent-fallback assertion: an unknown model must not come back with
        choices, whatever status accompanied it.
        """
        return isinstance(body, dict) and bool(body.get("choices"))


def usage_from(raw: dict[str, Any] | None) -> Usage:
    raw = raw or {}
    details = raw.get("completion_tokens_details")
    return Usage(
        prompt_tokens=raw.get("prompt_tokens"),
        completion_tokens=raw.get("completion_tokens"),
        total_tokens=raw.get("total_tokens"),
        reasoning_tokens=details.get("reasoning_tokens") if isinstance(details, dict) else None,
    )


_THINK = re.compile(r"<(think|thinking)>(.*?)(?:</\1>|\Z)", re.DOTALL | re.IGNORECASE)


def _inline_parts(content: str) -> tuple[str, str, str | None]:
    """Split reasoning once, retaining the observed tag for carrier assertions."""
    matches = list(_THINK.finditer(content)) if content else []
    if not matches:
        return content, "", None
    return (
        _THINK.sub("", content).strip(),
        "\n".join(match.group(2) for match in matches).strip(),
        matches[0].group(1).lower(),
    )


def split_inline_thinking(content: str) -> tuple[str, str]:
    """Split complete or budget-truncated <think>/<thinking> blocks."""
    text, reasoning, _ = _inline_parts(content)
    return text, reasoning


def reasoning_from(content: str, fields: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    """Return text, reasoning and its native origin without reparsing in assertions."""
    for name in ("reasoning_content", "reasoning"):
        value = fields.get(name)
        if isinstance(value, str) and value.strip():
            return content, value, "field", name
    text, reasoning, tag = _inline_parts(content)
    return text, reasoning, "inline" if tag else None, tag
