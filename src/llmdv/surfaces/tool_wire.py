"""Tool-calling wire shapes for the tools suite: definitions, choices, results and history.

Each generation surface with tools has one adapter here. Cases describe a tool turn with the
surface-neutral values below and never write a native tool field themselves, so one case runs
on Chat, Messages and Responses unchanged.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .base import Completion, Surface

if TYPE_CHECKING:
    from ..http.sse import Transcript
    from ..profile.schema import Model


@dataclass(frozen=True)
class ToolDef:
    """A function tool. `parameters=None` asks for the API's minimal no-argument form.

    A `description` of None leaves the optional field out. `strict` is sent only when set.
    `extra` adds native definition fields that only one API has, such as Messages'
    `input_examples`.
    """

    name: str
    description: str | None
    parameters: dict[str, Any] | None
    strict: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def schema(self) -> dict[str, Any]:
        """The schema the call's arguments must satisfy."""
        if self.parameters is None:
            return {"type": "object", "maxProperties": 0}
        return self.parameters


# `required` is Messages' `any`; `named` forces names[0]; `allowed` restricts to names.
TOOL_CHOICE_MODES = frozenset({"auto", "none", "required", "named", "allowed"})


@dataclass(frozen=True)
class ToolChoice:
    mode: str
    names: tuple[str, ...] = ()
    # For `allowed`: `required` must call one of names; `auto` may answer in text instead.
    allowed_mode: str = "required"

    def __post_init__(self) -> None:
        if self.mode not in TOOL_CHOICE_MODES:
            raise ValueError(f"unknown tool choice {self.mode!r}")
        if self.mode in ("named", "allowed") and not self.names:
            raise ValueError(f"tool choice {self.mode!r} needs at least one tool name")
        if self.allowed_mode not in ("auto", "required"):
            raise ValueError(f"unknown allowed-tools mode {self.allowed_mode!r}")


@dataclass(frozen=True)
class PriorCall:
    """A call the case writes into the history, as a client replaying a conversation does.

    String arguments are replayed verbatim, as the model emitted them, on the APIs that carry
    arguments as text.
    """

    id: str
    name: str
    arguments: dict[str, Any] | str

    @property
    def text(self) -> str:
        return self.arguments if isinstance(self.arguments, str) else json.dumps(self.arguments)


@dataclass(frozen=True)
class ToolOutput:
    """A result for one call. `call_id=None` omits the id; a tuple `content` is text parts.

    `blocks` are native content blocks a wire built for its own API, sent after any text.
    """

    call_id: str | None
    content: str | tuple[str, ...] = ""
    is_error: bool | None = None
    image_png: bytes | None = None
    blocks: tuple[dict[str, Any], ...] = ()


def _without(body: dict[str, Any], *names: str) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key not in names}


class ToolWire:
    """Builds tool requests in one surface's own shape."""

    surface: Surface
    name_limit: int
    id_pattern: str | None = None
    stream_keys: tuple[str, ...] = ("stream",)

    def __init__(self, surface: Surface) -> None:
        self.surface = surface

    # ------------------------------------------------------------------ finish reasons

    def call_finish(self, model: Model, choice: ToolChoice | None) -> str:
        raise NotImplementedError

    def text_finish(self) -> str:
        raise NotImplementedError

    def length_finish(self) -> str:
        """The normalized finish reason of a turn cut by the output token limit."""
        return "length"

    # ------------------------------------------------------------------ requests

    def definition(self, tool: ToolDef) -> dict[str, Any]:
        raise NotImplementedError

    def choice(self, choice: ToolChoice, parallel: bool | None) -> dict[str, Any]:
        """Request fields for a choice and the parallel-call switch."""
        raise NotImplementedError

    def request(
        self,
        model: str,
        prompt: str,
        tools: list[ToolDef],
        choice: ToolChoice | None,
        max_tokens: int,
        *,
        parallel: bool | None = None,
        stream: bool = False,
        sampled: bool = False,
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {"tools": [self.definition(tool) for tool in tools]}
        if choice is not None or parallel is not None:
            extra.update(self.choice(choice or ToolChoice("auto"), parallel))
        if stream:
            extra.update(self.stream_fields())
        body = self.surface.minimal_body(model, max_tokens, prompt, **extra)
        if sampled:
            body.pop("temperature", None)
        return body

    def raw_tool_request(
        self, model: str, prompt: str, tools: list[dict[str, Any]], max_tokens: int
    ) -> dict[str, Any]:
        """A request offering native tool entries the neutral definition cannot express."""
        return self.surface.minimal_body(model, max_tokens, prompt, tools=tools)

    def stream_fields(self) -> dict[str, Any]:
        return {"stream": True}

    def followup(
        self, body: dict[str, Any], completion: Completion, outputs: list[ToolOutput]
    ) -> dict[str, Any]:
        """The same request extended by the assistant's calls and their results."""
        raise NotImplementedError

    def history(
        self,
        model: str,
        prompt: str,
        tools: list[ToolDef],
        calls: list[PriorCall],
        outputs: list[ToolOutput],
        max_tokens: int,
        *,
        preface: str | None = None,
        after: str | None = None,
    ) -> dict[str, Any]:
        """One request whose history already holds the calls and these results.

        `preface` is assistant text before the calls, as clients replay it. `after` is user text
        that follows the results in the same turn, where clients put reminders.
        """
        raise NotImplementedError

    def assert_tool_stream(self, transcript: Transcript) -> None:
        """Tool-call stream checks beyond the surface's own lifecycle assertion."""


class ChatToolWire(ToolWire):
    name_limit = 64
    stream_keys = ("stream", "stream_options")

    def call_finish(self, model: Model, choice: ToolChoice | None) -> str:
        if choice is not None and choice.mode == "named":
            return model.tools.forced_finish_reason
        return "tool_calls"

    def text_finish(self) -> str:
        return "stop"

    def definition(self, tool: ToolDef) -> dict[str, Any]:
        function: dict[str, Any] = {"name": tool.name, "description": tool.description}
        if tool.description is None:
            del function["description"]
        if tool.parameters is not None:
            function["parameters"] = tool.parameters
        if tool.strict is not None:
            function["strict"] = tool.strict
        function.update(tool.extra)
        return {"type": "function", "function": function}

    def choice(self, choice: ToolChoice, parallel: bool | None) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if choice.mode == "named":
            fields["tool_choice"] = {"type": "function", "function": {"name": choice.names[0]}}
        elif choice.mode == "allowed":
            fields["tool_choice"] = {
                "type": "allowed_tools",
                "allowed_tools": {
                    "mode": choice.allowed_mode,
                    "tools": [{"type": "function", "function": {"name": n}} for n in choice.names],
                },
            }
        else:
            fields["tool_choice"] = choice.mode
        if parallel is not None:
            fields["parallel_tool_calls"] = parallel
        return fields

    def stream_fields(self) -> dict[str, Any]:
        return {"stream": True, "stream_options": {"include_usage": True}}

    def custom_tool(self, name: str, description: str) -> dict[str, Any]:
        return {"type": "custom", "custom": {"name": name, "description": description}}

    def followup(self, body, completion, outputs):
        return {
            **_without(body, *self.stream_keys),
            "messages": [
                *body["messages"],
                completion.assistant_message,
                *(self._result(output) for output in outputs),
            ],
        }

    def history(
        self, model, prompt, tools, calls, outputs, max_tokens, *, preface=None, after=None
    ):
        body = self.request(model, prompt, tools, None, max_tokens)
        body["messages"] += [
            {
                "role": "assistant",
                "content": preface,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.text},
                    }
                    for call in calls
                ],
            },
            *(self._result(output) for output in outputs),
        ]
        if after is not None:
            body["messages"].append({"role": "user", "content": after})
        return body

    @staticmethod
    def _result(output: ToolOutput) -> dict[str, Any]:
        if output.image_png is not None or output.blocks:
            raise ValueError("a Chat tool message carries text parts only")
        content = output.content
        message: dict[str, Any] = {
            "role": "tool",
            "content": [{"type": "text", "text": part} for part in content]
            if isinstance(content, tuple)
            else content,
        }
        if output.call_id is not None:
            message["tool_call_id"] = output.call_id
        return message

    def assert_tool_stream(self, transcript: Transcript) -> None:
        from ..assertions.chat_tools import assert_chat_tool_deltas
        from ..assertions.tool_calls import assert_chat_tool_openers

        frames = [event.json() for event in transcript.data_events]
        assert_chat_tool_deltas(frames)
        assert_chat_tool_openers(frames)


class MessagesToolWire(ToolWire):
    name_limit = 128
    # The native API requires tool_use ids a client sends back to match this pattern.
    id_pattern = r"[a-zA-Z0-9_-]+"

    def call_finish(self, model: Model, choice: ToolChoice | None) -> str:
        return "tool_use"

    def text_finish(self) -> str:
        return "end_turn"

    def length_finish(self) -> str:
        return "max_tokens"

    def definition(self, tool: ToolDef) -> dict[str, Any]:
        native: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            # The wire shape requires input_schema; a bare object schema means no parameters.
            "input_schema": tool.parameters if tool.parameters is not None else {"type": "object"},
        }
        if tool.description is None:
            del native["description"]
        if tool.strict is not None:
            native["strict"] = tool.strict
        native.update(tool.extra)
        return native

    def choice(self, choice: ToolChoice, parallel: bool | None) -> dict[str, Any]:
        if choice.mode == "allowed":
            raise ValueError("Messages has no allowed-tools choice")
        native: dict[str, Any] = (
            {"type": "tool", "name": choice.names[0]}
            if choice.mode == "named"
            else {"type": "any"}
            if choice.mode == "required"
            else {"type": choice.mode}
        )
        if parallel is not None:
            if choice.mode == "none":
                raise ValueError("Messages cannot set disable_parallel_tool_use with none")
            native["disable_parallel_tool_use"] = not parallel
        return {"tool_choice": native}

    def request(self, model, prompt, tools, choice, max_tokens, **kw):
        body = super().request(model, prompt, tools, choice, max_tokens, **kw)
        if choice is not None and choice.mode in ("required", "named"):
            # A forced choice is sent without a caller-selected temperature, as clients do.
            body.pop("temperature", None)
        return body

    def custom_tool(self, name: str, description: str) -> dict[str, Any]:
        """The explicit `type: custom` form of an ordinary client tool."""
        return {
            "type": "custom",
            "name": name,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": {"word": {"type": "string"}},
                "required": ["word"],
            },
        }

    def server_tool(self, kind: str) -> dict[str, Any]:
        """A typed tool entry the API executes itself or defines by schema version."""
        return dict(SERVER_TOOLS[kind])

    def followup(self, body, completion, outputs):
        return {
            **_without(body, *self.stream_keys),
            "messages": [
                *body["messages"],
                {"role": "assistant", "content": completion.raw["content"]},
                {"role": "user", "content": [self._result(output) for output in outputs]},
            ],
        }

    def history(
        self, model, prompt, tools, calls, outputs, max_tokens, *, preface=None, after=None
    ):
        body = self.request(model, prompt, tools, None, max_tokens)
        if any(isinstance(call.arguments, str) for call in calls):
            raise ValueError("Messages tool_use input is an object")
        uses = [
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            for call in calls
        ]
        # Results come first in the user turn; any text follows them, as the API requires.
        results = [self._result(output) for output in outputs]
        body["messages"] += [
            {
                "role": "assistant",
                "content": ([{"type": "text", "text": preface}] if preface else []) + uses,
            },
            {
                "role": "user",
                "content": results + ([{"type": "text", "text": after}] if after else []),
            },
        ]
        return body

    def result_block(self, kind: str, title: str, text: str) -> dict[str, Any]:
        """A non-text tool_result content block: a search result or a plain-text document."""
        if kind == "search_result":
            return {
                "type": "search_result",
                "source": "https://example.com/report",
                "title": title,
                "content": [{"type": "text", "text": text}],
            }
        if kind == "document":
            return {
                "type": "document",
                "title": title,
                "source": {"type": "text", "media_type": "text/plain", "data": text},
            }
        raise ValueError(f"no tool_result block kind {kind!r}")

    @staticmethod
    def _result(output: ToolOutput) -> dict[str, Any]:
        content = output.content
        parts: Any = (
            [{"type": "text", "text": part} for part in content]
            if isinstance(content, tuple)
            else content
        )
        if output.image_png is not None:
            image = {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(output.image_png).decode("ascii"),
                },
            }
            parts = [*([{"type": "text", "text": parts}] if isinstance(parts, str) else parts)]
            parts.append(image)
        if output.blocks:
            parts = [*([{"type": "text", "text": parts}] if parts else []), *output.blocks]
        block: dict[str, Any] = {"type": "tool_result", "content": parts}
        if output.call_id is not None:
            block["tool_use_id"] = output.call_id
        if output.is_error is not None:
            block["is_error"] = output.is_error
        return block


# Typed Messages tools a client such as Claude Code sends, each at a published schema version.
SERVER_TOOLS: dict[str, dict[str, Any]] = {
    "web_search": {"type": "web_search_20250305", "name": "web_search", "max_uses": 1},
    "web_fetch": {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 1},
    "code_execution": {"type": "code_execution_20250825", "name": "code_execution"},
    "bash": {"type": "bash_20250124", "name": "bash"},
    "text_editor": {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
    "memory": {"type": "memory_20250818", "name": "memory"},
}


class ResponsesToolWire(ToolWire):
    name_limit = 64

    def call_finish(self, model: Model, choice: ToolChoice | None) -> str:
        # Responses reports a status; a turn that calls a function completes normally.
        return "stop"

    def text_finish(self) -> str:
        return "stop"

    def definition(self, tool: ToolDef) -> dict[str, Any]:
        # The native type requires the parameters key; null means no parameters.
        native: dict[str, Any] = {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        if tool.description is None:
            del native["description"]
        if tool.strict is not None:
            native["strict"] = tool.strict
        native.update(tool.extra)
        return native

    def choice(self, choice: ToolChoice, parallel: bool | None) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if choice.mode == "named":
            fields["tool_choice"] = {"type": "function", "name": choice.names[0]}
        elif choice.mode == "allowed":
            fields["tool_choice"] = {
                "type": "allowed_tools",
                "mode": choice.allowed_mode,
                "tools": [{"type": "function", "name": name} for name in choice.names],
            }
        else:
            fields["tool_choice"] = choice.mode
        if parallel is not None:
            fields["parallel_tool_calls"] = parallel
        return fields

    def custom_tool(self, name: str, description: str) -> dict[str, Any]:
        return {
            "type": "custom",
            "name": name,
            "description": description,
            "format": {"type": "grammar", "syntax": "regex", "definition": r"[a-z]{1,12}"},
        }

    def namespace_tool(self, name: str, tools: list[ToolDef]) -> dict[str, Any]:
        return {
            "type": "namespace",
            "name": name,
            "description": f"Tools of the {name} service.",
            "tools": [self.definition(tool) for tool in tools],
        }

    def followup(self, body, completion, outputs):
        prior = body["input"]
        return {
            **_without(body, *self.stream_keys),
            "input": [
                *([{"role": "user", "content": prior}] if isinstance(prior, str) else prior),
                *completion.raw["output"],
                *(self._result(output) for output in outputs),
            ],
        }

    def limit_calls(self, body: dict[str, Any], count: int) -> dict[str, Any]:
        """Set the built-in-tool limit; it does not cap function-tool calls."""
        return {**body, "max_tool_calls": count}

    def history(
        self, model, prompt, tools, calls, outputs, max_tokens, *, preface=None, after=None
    ):
        body = self.request(model, prompt, tools, None, max_tokens)
        said = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": preface}],
            }
        ]
        body["input"] = [
            {"role": "user", "content": prompt},
            *(said if preface else []),
            *(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": call.text,
                }
                for call in calls
            ),
            *(self._result(output) for output in outputs),
            *([{"role": "user", "content": after}] if after else []),
        ]
        return body

    @staticmethod
    def _result(output: ToolOutput) -> dict[str, Any]:
        if output.blocks:
            raise ValueError("Responses results carry text and image parts only")
        content = output.content
        value: Any = (
            [{"type": "input_text", "text": part} for part in content]
            if isinstance(content, tuple)
            else content
        )
        if output.image_png is not None:
            encoded = base64.b64encode(output.image_png).decode("ascii")
            value = [
                *([{"type": "input_text", "text": value}] if isinstance(value, str) else value)
            ]
            # `detail` is required of an input image, and engines validate it.
            image = {"type": "input_image", "image_url": f"data:image/png;base64,{encoded}"}
            value.append({**image, "detail": "auto"})
        item: dict[str, Any] = {"type": "function_call_output", "output": value}
        if output.call_id is not None:
            item["call_id"] = output.call_id
        return item


_WIRES = {
    "chat_completions": ChatToolWire,
    "messages": MessagesToolWire,
    "responses": ResponsesToolWire,
}


def tool_wire(surface: Surface) -> ToolWire:
    """The tool adapter for a surface that declares the tools feature."""
    try:
        return _WIRES[surface.name](surface)
    except KeyError:
        raise KeyError(f"no tool wire shape for surface {surface.name!r}") from None
