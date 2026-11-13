"""Request variants for the error-path cases, written in each surface's own shape.

Each variant is one ordinary client mistake or edge: an empty input list, an unknown role, no
model, a tool choice without tools, a token limit past the window, an image to a text-only
model. A body in one surface's shape is a garbled request on another, so each surface gets its
own body, and a surface without the concept gets none. `body` returns None there, and the case
skips it as not implemented.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import DEFAULT_PROMPT, Surface

# A 1x1 PNG: a valid image of the smallest size, for the text-only-model check.
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9a"
    "wAAAABJRU5ErkJggg=="
)
NOT_BASE64 = "not-base64!"
GENERATION = ("chat_completions", "completions", "messages", "responses")

Body = dict[str, Any]


def _input(body: Body) -> str:
    return "input" if "input" in body else "messages"


def _empty_input(name: str, body: Body) -> Body | None:
    if name == "completions":
        return None
    body[_input(body)] = []
    return body


def _invalid_role(name: str, body: Body) -> Body | None:
    if name == "completions":
        return None
    body[_input(body)] = [{"role": "wizard", "content": DEFAULT_PROMPT}]
    return body


def _missing_model(name: str, body: Body) -> Body | None:
    del body["model"]
    return body


def _tool_choice_without_tools(name: str, body: Body) -> Body | None:
    if name == "completions":
        return None
    body["tool_choice"] = {"type": "any"} if name == "messages" else "required"
    return body


def _missing_token_limit(name: str, body: Body) -> Body | None:
    # Only the Messages API requires a token limit.
    if name != "messages":
        return None
    del body["max_tokens"]
    return body


def _image(name: str, body: Body, data: str, in_tool_result: bool = False) -> Body | None:
    url = f"data:image/png;base64,{data}"
    if name == "messages":
        image = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": data},
        }
        if not in_tool_result:
            body["messages"] = [{"role": "user", "content": [image, _text(DEFAULT_PROMPT)]}]
            return body
        body["tools"] = [
            {"name": "screenshot", "description": "Take a screenshot.",
             "input_schema": {"type": "object", "properties": {}}}
        ]  # fmt: skip
        body["messages"] = [
            {"role": "user", "content": "Describe the screen."},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_llmdv", "name": "screenshot", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_llmdv", "content": [image]}]},
        ]  # fmt: skip
        return body
    if in_tool_result:
        return None
    if name == "chat_completions":
        image = {"type": "image_url", "image_url": {"url": url}}
        body["messages"] = [{"role": "user", "content": [image, _text(DEFAULT_PROMPT)]}]
        return body
    if name == "responses":
        image = {"type": "input_image", "image_url": url}
        body["input"] = [
            {"role": "user", "content": [image, {"type": "input_text", "text": DEFAULT_PROMPT}]}
        ]
        return body
    return None


def _text(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _invalid_thinking_type(name: str, body: Body) -> Body | None:
    if name != "messages":
        return None
    body["thinking"] = {"type": "not-valid"}
    return body


def _server_tool(name: str, body: Body) -> Body | None:
    if name != "messages":
        return None
    body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}]
    return body


def _sampling_option(name: str, body: Body) -> Body | None:
    if name not in ("chat_completions", "completions"):
        return None
    body.update(min_p=0.1, logit_bias={"0": -1})
    return body


VARIANTS: dict[str, Callable[[str, Body], Body | None]] = {
    "empty_input": _empty_input,
    "invalid_role": _invalid_role,
    "missing_model": _missing_model,
    "tool_choice_without_tools": _tool_choice_without_tools,
    "missing_token_limit": _missing_token_limit,
    "invalid_image": lambda name, body: _image(name, body, NOT_BASE64),
    "image_input": lambda name, body: _image(name, body, PNG_1X1),
    "image_in_tool_result": lambda name, body: _image(name, body, PNG_1X1, in_tool_result=True),
    "invalid_thinking_type": _invalid_thinking_type,
    "server_tool": _server_tool,
    "sampling_option": _sampling_option,
}


def body(surface: Surface, model: str, variant: str, max_tokens: int) -> Body | None:
    """The surface's body for `variant`, or None when the surface has no such request."""
    if surface.name not in GENERATION:
        return None
    # A variant that may be served requires output, so it starts from the surface's reply prompt.
    body = surface.minimal_body(model, max_tokens=max_tokens, prompt=surface.reply_prompt)
    return VARIANTS[variant](surface.name, body)
