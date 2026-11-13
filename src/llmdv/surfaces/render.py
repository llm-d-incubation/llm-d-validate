"""Render produces engine inputs, not generations; only render-feature cases drive it.

Requests delegate to their API's adapter. Response grammar is GenerateRequest, with a batch
array for completions. There is deliberately no Completion or stream normalization here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import DEFAULT_PROMPT, SURFACE_PATHS, Surface, SurfaceKind, register
from .chat_completions import CHAT_COMPLETIONS
from .completions import COMPLETIONS
from .messages import MESSAGES
from .openai import OpenAIErrors
from .responses import RESPONSES


@dataclass(frozen=True)
class RenderedPrompt:
    token_ids: Any
    model: Any
    request_id: Any
    sampling_params: Any


class Render(OpenAIErrors):
    kind = SurfaceKind.RENDER
    features = frozenset(
        {
            "render",
        }
    )

    def __init__(self, name: str, request_surface: Surface, *, batched: bool = False) -> None:
        self.name = name
        self.path = SURFACE_PATHS[name]
        self.request_surface = request_surface
        self.batched = batched

    def minimal_body(
        self, model: str, max_tokens: int = 8, prompt: str = DEFAULT_PROMPT, **extra
    ) -> dict[str, Any]:
        return self.request_surface.minimal_body(model, max_tokens, prompt, **extra)

    def extra_headers(self) -> dict[str, str]:
        return self.request_surface.extra_headers()

    def parse_render(self, body: Any) -> RenderedPrompt:
        if self.batched:
            body = body[0] if isinstance(body, list) and len(body) == 1 else None
        body = body if isinstance(body, dict) else {}
        return RenderedPrompt(
            body.get("token_ids"),
            body.get("model"),
            body.get("request_id"),
            body.get("sampling_params"),
        )

    def looks_like_success(self, body: Any) -> bool:
        return bool(self.parse_render(body).token_ids)


CHAT_RENDER = register(Render("chat_render", CHAT_COMPLETIONS))
COMPLETIONS_RENDER = register(Render("completions_render", COMPLETIONS, batched=True))
MESSAGES_RENDER = register(Render("messages_render", MESSAGES))
RESPONSES_RENDER = register(Render("responses_render", RESPONSES))
