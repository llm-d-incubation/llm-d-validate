"""A generic tool-calling model behind the fake fleet, for the tools suite.

It reads the offered tools, the tool choice, the parallel-call switch and any results in the
history, the way an engine with a tool parser does, and answers in each API's own shape:
Chat Completions, Messages and Responses, streamed or not. Arguments are built from each tool's
JSON Schema and the prompt, so a case that asks for a value in its prompt gets it back.
It also reads images, in a user turn or a result, and names the stripes a striped one shows.

`defect` reproduces one misbehaviour the tools cases exist to catch; DEFECTS names them all.
It may instead name one of BEHAVIOURS, a healthy variation of the model that the cases must
accept.
"""

from __future__ import annotations

import base64
import binascii
import itertools
import json
import re
import struct
import zlib
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

DEFECTS = frozenset(
    {
        "truncated_as_call",  # a call cut by the token limit is reported as a finished call
        "rejects_text_beside_calls",  # assistant text beside replayed calls is refused
        "drops_trailing_text",  # user text after a result never reaches the model
        "drops_result_blocks",  # search_result and document blocks in a result are dropped
        "accepts_orphans",  # serves a result whose call id names no issued call
        "allowed_5xx",  # allowed_tools fails with a server error
        "args_as_strings",  # integers in arguments arrive as strings
        "auto_always_calls",  # auto calls a tool for a prompt that needs none
        "bad_arguments_5xx",  # replayed argument text that is not a JSON object fails with a 5xx
        "bad_messages_id",  # Messages tool_use ids outside the native id pattern
        "call_as_markup",  # a call comes back as raw call markup in the text, with no call
        "count_ignores_tools",  # count_tokens leaves the tool list out
        "cuts_long_stream_arguments",  # streamed arguments stop at 256 characters
        "description_required_5xx",  # a tool without a description fails with a 5xx
        "doubles_backslashes",  # backslashes in string arguments arrive doubled
        "forced_stream_as_text",  # a streamed named call arrives as answer text
        "format_blocks_calls",  # an output format constrains the call turn, so no call is made
        "format_dropped_after_result",  # the answer after a result ignores the output format
        "history_without_tools_5xx",  # replayed calls with no tools offered fail with a 5xx
        "no_call_after_result",  # a turn after a result never calls a tool
        "omitted_content_5xx",  # a Chat call turn replayed without content fails with a 5xx
        "big_int_rounded",  # integers above 2^53 pass through a float
        "drops_is_error",  # tool_result is_error never reaches the prompt
        "drops_parts",  # a result sent as content parts arrives empty
        "count_ignores_image",  # count_tokens leaves a user image out
        "count_image_as_text",  # count_tokens counts a user image as its base64 text
        "count_poisons_image",  # the first generation after counting an image fails with a 5xx
        "drops_result_image",  # an image in a result never reaches the model
        "drops_second_image",  # only the first image in a user turn reaches the model
        "drops_user_image",  # an image in a user turn never reaches the model
        "fetches_image_urls",  # an image URL inside the fleet is fetched, and the failure is a 5xx
        "large_image_413",  # a user image above 1 MB is refused as too large
        "text_keyed_answer",  # the answer is reused for any request with the same text
        "drops_second_result",  # only the first result in a turn reaches the model
        "drops_third_call",  # at most two calls per turn
        "duplicate_5xx",  # duplicate tool names fail with a server error
        "markup_grammar_5xx",  # a schema answer after a result quoting <tool_call> is a 5xx
        "markup_empty_answer",  # a schema answer after a result quoting <tool_call> is empty
        "duplicate_ids",  # every call in a turn shares one id
        "empty_arguments",  # a call to a tool without parameters has empty argument text
        "empty_result_5xx",  # an empty result fails with a server error
        "escapes_unicode",  # results reach the model with non-ASCII escaped
        "ignores_allowed",  # allowed_tools is ignored
        "ignores_legacy_functions",  # the legacy functions field is ignored and text returned
        "ignores_named",  # a named choice calls the first tool
        "ignores_parallel_off",  # parallel calls disabled still returns several calls
        "ignores_result",  # the answer after a result ignores it
        "image_as_text",  # an image in a result is rendered as its base64 text
        "index_ids",  # call ids count from zero in each response, so they repeat
        "input_examples_5xx",  # input_examples fails with a server error
        "markup_in_text",  # raw call markup leaks into call or result-answer text
        "merged_stream",  # several streamed calls merge into one
        "missing_result_5xx",  # a turn that answers only some of its calls fails with a 5xx
        "merges_parallel",  # several calls merge into one with concatenated arguments
        "n_second_as_text",  # with several streamed Chat choices, the second's call is text
        "named_no_parameters_5xx",  # a named choice of a tool without properties fails
        "none_empty",  # tool_choice none answers with empty text
        "none_markup",  # tool_choice none answers with raw call markup
        "opener_without_name",  # a streamed Chat call's first fragment has no name
        "rejects_allowed",  # allowed_tools is refused with a 400
        "rejects_schema_keywords",  # $schema, title, format or pattern in a schema is refused
        "rejects_strict",  # strict on a tool is refused
        "rejects_thinking_replay",  # a replayed thinking block is refused with a 400
        "renames_dotted",  # names outside the spec come back sanitized
        "reorders_code_lines",  # source-code argument lines arrive in the wrong order
        "ends_code_with_blank_line",  # source-code arguments gain a blank line at the end
        "schema_violation",  # arguments miss a required property
        "stop_finish",  # a call turn ends with the text finish reason
        "strips_strict_keys",  # every key named strict is removed from tool schemas
        "stream_args_differ",  # streamed arguments differ from the non-streamed ones
        "tool_type_5xx",  # a non-function tool type fails with a server error
        "truncates_description",  # descriptions are cut at 2,000 characters
        "truncates_names",  # names are cut at 40 characters
        "truncates_results",  # each result is cut at 4,000 characters
        "truncates_tools",  # only the first 32 tools are offered to the model
        "unsigned_thinking",  # thinking blocks beside calls carry no signature
    }
)

# Healthy variations of the model, which the cases must accept.
BEHAVIOURS = frozenset(
    {
        "ends_code_with_newline",  # written source code ends its last line with a newline
    }
)

PATHS = {"/v1/chat/completions": "chat", "/v1/messages": "messages", "/v1/responses": "responses"}
COUNT_PATH = "/v1/messages/count_tokens"
CITIES = ("Paris", "Tokyo", "Zürich", "Lyon")
STRING_FORMATS = {
    "date": r"\d{4}-\d{2}-\d{2}",
    "email": r"[\w.+-]+@[\w-]+\.[\w.]*\w",
    "uuid": r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
}
_ids = itertools.count(1)
# What the stateful defects remember between requests: images counted, and answers by text.
_COUNTED: set[str] = set()
_ANSWERS: dict[str, str] = {}


@dataclass
class Function:
    name: str
    description: str
    schema: dict[str, Any]
    native: dict[str, Any]


@dataclass
class Result:
    call_id: str | None
    text: str
    is_error: bool = False
    image: str | None = None


@dataclass
class Turn:
    text: str = ""
    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    finish: str | None = None
    input_tokens: int = 3
    # Messages thinking, given its own block before the text and calls.
    thinking: str = ""
    signature: str = ""


def handles(path: str, body: dict) -> bool:
    """Whether the engine serves this request: a tools path offering tools or replaying calls."""
    if path == COUNT_PATH:
        return bool(body.get("tools") or _user_images("messages", body))
    if path not in PATHS:
        return False
    legacy = path == "/v1/chat/completions" and "functions" in body
    if body.get("tools") or legacy or _history(PATHS[path], body).calls:
        return True
    return bool(_user_parts(PATHS[path], body))


def serve(
    path: str, body: dict, model: str, defect: str | None, warm_cache: bool = False
) -> tuple[int, Any, str | None]:
    """(status, JSON payload, SSE text or None) for one request.

    `warm_cache` reports a Messages prompt as a warm prefix cache does: all but its last two
    tokens read from cache, so input_tokens is only the uncached tail of the same total.
    """
    if defect is not None and defect not in DEFECTS | BEHAVIOURS:
        raise ValueError(f"unknown tool engine defect {defect!r}")
    if path == COUNT_PATH and not body.get("tools"):
        _COUNTED.update(_user_images("messages", body))
        return 200, {"input_tokens": _image_prompt_tokens("messages", body, defect)}, None
    if path == COUNT_PATH:
        # A synthetic count that grows with everything the template renders.
        counted = body
        if defect == "count_ignores_tools":
            counted = {key: value for key, value in body.items() if key != "tools"}
        return 200, {"input_tokens": len(json.dumps(counted).split())}, None
    api = PATHS[path]
    outcome = _turn(api, body, defect)
    if isinstance(outcome, tuple):
        status, message = outcome
        error_type = "invalid_request_error" if status < 500 else "server_error"
        return status, {"error": {"message": message, "type": error_type}}, None
    if defect == "index_ids":
        prefix = "toolu" if api == "messages" else "call"
        outcome.calls = [
            (f"{prefix}_{i}", name, args) for i, (_, name, args) in enumerate(outcome.calls)
        ]
    if api == "messages" and (body.get("thinking") or {}).get("type") == "enabled":
        outcome.thinking = "The prompt names a tool, so I call it."
        outcome.signature = "" if defect == "unsigned_thinking" else "c2lnbmVk"
    payload = _payload(api, model, outcome)
    if warm_cache and api == "messages":
        usage = payload["usage"]
        tail = min(2, usage["input_tokens"])
        usage["cache_read_input_tokens"] = usage["input_tokens"] - tail
        usage["cache_creation_input_tokens"] = 0
        usage["input_tokens"] = tail
    count = body.get("n") if api == "chat" and isinstance(body.get("n"), int) else 1
    for index in range(1, count):
        # Each choice is decoded on its own, so each gets its own calls and ids.
        extra = _payload(api, model, _turn(api, body, defect))["choices"][0]
        payload["choices"].append({**extra, "index": index})
    if body.get("stream"):
        return 200, payload, _stream(api, body, payload, defect)
    return 200, payload, None


# ---------------------------------------------------------------------------- reading a request


def _function(api: str, tool: Any) -> Function | None:
    if not isinstance(tool, dict):
        return None
    if api == "chat":
        function = tool.get("function")
        if tool.get("type") != "function" or not isinstance(function, dict):
            return None
        return Function(
            function.get("name"),
            function.get("description") or "",
            function.get("parameters") or {},
            function,
        )
    if api == "messages":
        if tool.get("type") not in (None, "custom") or "input_schema" not in tool:
            return None
        return Function(tool.get("name"), tool.get("description") or "", tool["input_schema"], tool)
    if tool.get("type") != "function":
        return None
    return Function(
        tool.get("name"), tool.get("description") or "", tool.get("parameters") or {}, tool
    )


def _choice(api: str, body: dict) -> tuple[str, tuple[str, ...], bool]:
    """(mode, names, parallel allowed) in neutral terms."""
    choice = body.get("tool_choice")
    parallel = body.get("parallel_tool_calls") is not False
    if api == "messages":
        choice = choice or {"type": "auto"}
        parallel = not choice.get("disable_parallel_tool_use", False)
        kind = choice.get("type")
        mode = {"any": "required", "tool": "named"}.get(kind, kind)
        return mode, (choice.get("name"),) if kind == "tool" else (), parallel
    if choice is None or isinstance(choice, str):
        return choice or "auto", (), parallel
    if choice.get("type") == "allowed_tools":
        allowed = choice.get("allowed_tools", choice)
        names = tuple(
            (tool.get("function") or tool).get("name") for tool in allowed.get("tools") or []
        )
        mode = "allowed_auto" if allowed.get("mode") == "auto" else "allowed"
        return mode, names, parallel
    name = (choice.get("function") or choice).get("name")
    return "named", (name,), parallel


def _text_of(content: Any, defect: str | None) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or defect == "drops_parts":
        return ""
    texts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text"):
            texts.append(part.get("text", ""))
        elif kind in ("search_result", "document") and defect != "drops_result_blocks":
            source = part.get("source")
            inner = part.get("content") if kind == "search_result" else [source]
            texts += [
                item.get("text") or item.get("data") or ""
                for item in inner
                if isinstance(item, dict)
            ]
    return "".join(texts)


def _images(content: Any) -> str | None:
    """The base64 data of the first image among content parts, in any API's part shape."""
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image":
            return (part.get("source") or {}).get("data", "")
        if part.get("type") == "input_image":
            return (part.get("image_url") or "").partition(",")[2]
        if part.get("type") == "image_url":
            return ((part.get("image_url") or {}).get("url") or "").partition(",")[2]
    return None


def _user_parts(api: str, body: dict) -> list[dict]:
    """The image parts user turns carry directly, outside any tool result, in order."""
    turns = body.get("input") if api == "responses" else body.get("messages")
    found = []
    for turn in turns if isinstance(turns, list) else []:
        content = turn.get("content") if isinstance(turn, dict) else None
        if (
            not isinstance(turn, dict)
            or turn.get("role") != "user"
            or not isinstance(content, list)
        ):
            continue
        kinds = ("image", "input_image", "image_url")
        found += [part for part in content if isinstance(part, dict) and part.get("type") in kinds]
    return found


def _image_url(part: dict) -> str | None:
    """The URL of an image part that names one, rather than carrying its data."""
    url = part.get("image_url")
    url = url.get("url") if isinstance(url, dict) else url
    url = url or (part.get("source") or {}).get("url")
    return url if isinstance(url, str) and not url.startswith("data:") else None


def _user_images(api: str, body: dict) -> list[str]:
    """The base64 data of each image the user turns carry, in order."""
    return [image for part in _user_parts(api, body) if (image := _images([part]))]


def _image_prompt_tokens(api: str, body: dict, defect: str | None) -> int:
    """A synthetic size of an image turn: its words, and 300 tokens for each image."""
    images = _user_images(api, body)
    text = json.dumps(body.get("input") if api == "responses" else body.get("messages"))
    for image in images:
        text = text.replace(image, "")
    each = {"count_ignores_image": 0, "count_image_as_text": None}.get(defect, 300)
    return len(text.split()) + sum(len(i) // 3 if each is None else each for i in images)


def _image_turn(api: str, body: dict, defect: str | None) -> Turn | tuple[int, str]:
    """The answer to a user turn that carries images and offers no tools."""
    parts = _user_parts(api, body)
    if any(_image_url(part) for part in parts):
        if defect == "fetches_image_urls":
            return 500, "Cannot connect to host"
        return 400, "image URLs outside the allowed domains are refused"
    images = _user_images(api, body)
    if defect == "large_image_413" and any(len(image) > 1_000_000 for image in images):
        return 413, "request entity too large"
    if defect == "count_poisons_image" and _COUNTED & set(images):
        _COUNTED.difference_update(images)
        return 500, "Internal server error"
    seen = images[:1] if defect == "drops_second_image" else images
    shown = [] if defect == "drops_user_image" else [_stripes(image) for image in seen]
    answer = ", ".join(name for name in shown if name) or "ok"
    if defect == "text_keyed_answer":
        text = json.dumps(body.get("input") if api == "responses" else body.get("messages"))
        for image in images:
            text = text.replace(image, "")
        answer = _ANSWERS.setdefault(text, answer)
    return Turn(text=answer, input_tokens=_image_prompt_tokens(api, body, None))


# What a vision model can name. Held here, not imported from llmdv, so the engine reads the
# picture for itself instead of repeating the answer the case expects.
PALETTE = {
    (255, 0, 0): "red",
    (0, 160, 0): "green",
    (0, 0, 255): "blue",
    (255, 255, 0): "yellow",
    (0, 0, 0): "black",
}


def _stripes(image: str) -> str | None:
    """The colours of a striped PNG from left to right, or None for any other picture."""
    try:
        png = base64.b64decode(image, validate=True)
        width, height, depth, kind = struct.unpack(">IIBB", png[16:26])
        data, offset = b"", 8
        while offset < len(png):
            (length,) = struct.unpack(">I", png[offset : offset + 4])
            if png[offset + 4 : offset + 8] == b"IDAT":
                data += png[offset + 8 : offset + 8 + length]
            offset += length + 12
        row = zlib.decompress(data)[: 1 + width * 3]
    except (binascii.Error, struct.error, zlib.error):
        return None
    if (depth, kind) != (8, 2) or len(row) != 1 + width * 3 or row[0] != 0:
        return None
    names: list[str] = []
    for start in range(1, len(row), 3):
        name = PALETTE.get(tuple(row[start : start + 3]))
        if name is None:
            return None
        if name not in names[-1:]:
            names.append(name)
    return ", ".join(names)


@dataclass
class History:
    calls: dict[str, str] = field(default_factory=dict)
    results: list[Result] = field(default_factory=list)
    prompt: list[str] = field(default_factory=list)
    # User text that follows a result, where clients put reminders.
    after: list[str] = field(default_factory=list)
    # Whether an assistant turn carried text beside its calls.
    text_beside_calls: bool = False

    def user(self, text: str) -> None:
        self.prompt.append(text)
        if self.results:
            self.after.append(text)


def _history(api: str, body: dict, defect: str | None = None) -> History:
    """Calls issued earlier, results sent back, and the user's text, in order."""
    seen = History()
    if api == "responses":
        items = body.get("input")
        if isinstance(items, str):
            seen.user(items)
            return seen
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                seen.calls[item.get("call_id")] = item.get("name")
            elif item.get("type") == "function_call_output":
                output = item.get("output")
                seen.results.append(
                    Result(item.get("call_id"), _text_of(output, defect), image=_images(output))
                )
            elif item.get("role") == "user":
                seen.user(_text_of(item.get("content"), None))
            elif item.get("role") == "assistant":
                seen.text_beside_calls = True
        return seen
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role, content = message.get("role"), message.get("content")
        if api == "chat":
            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    seen.calls[call.get("id")] = (call.get("function") or {}).get("name")
                seen.text_beside_calls |= bool(content and message.get("tool_calls"))
            elif role == "tool":
                seen.results.append(Result(message.get("tool_call_id"), _text_of(content, defect)))
            elif role == "user":
                seen.user(_text_of(content, None))
            continue
        blocks = content if isinstance(content, list) else []
        if role == "assistant":
            kinds = {block.get("type") for block in blocks if isinstance(block, dict)}
            seen.text_beside_calls |= {"text", "tool_use"} <= kinds
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    seen.calls[block.get("id")] = block.get("name")
        elif role == "user":
            if isinstance(content, str):
                seen.user(content)
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    inner = block.get("content")
                    seen.results.append(
                        Result(
                            block.get("tool_use_id"),
                            _text_of(inner, defect),
                            bool(block.get("is_error")),
                            _images(inner),
                        )
                    )
                elif block.get("type") == "text":
                    seen.user(block.get("text", ""))
    return seen


def _input_tokens(body: dict, results: list[Result], defect: str | None) -> int:
    """A synthetic prompt size that grows with what the template renders."""
    view = _drop_key(body, "is_error") if defect == "drops_is_error" else body
    text = json.dumps(view)
    image_tokens = 0
    for result in results:
        if result.image:
            text = text.replace(result.image, "")
            image_tokens += len(result.image) // 3 if defect == "image_as_text" else 300
    return len(text.split()) + image_tokens


def _drop_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return {k: _drop_key(v, key) for k, v in value.items() if k != key}
    if isinstance(value, list):
        return [_drop_key(item, key) for item in value]
    return value


# ---------------------------------------------------------------------------- deciding a turn


def _turn(api: str, body: dict, defect: str | None) -> Turn | tuple[int, str]:
    if api == "chat" and "functions" in body and not body.get("tools"):
        # The engine's request schema has no legacy functions field.
        if defect == "ignores_legacy_functions":
            return Turn(text="It is mild and clear today.")
        return 400, "functions is not supported; use tools"
    if _user_parts(api, body) and not body.get("tools") and not _history(api, body).calls:
        return _image_turn(api, body, defect)
    functions = []
    for tool in body.get("tools") or []:
        function = _function(api, tool)
        if function is None:
            if defect == "tool_type_5xx":
                return 500, "internal error while rendering tools"
            return 400, "only function tools are supported"
        functions.append(function)
    if defect == "strips_strict_keys":
        for function in functions:
            function.schema = _drop_key(function.schema, "strict")
    names = [function.name for function in functions]
    if len(set(names)) != len(names):
        return (500, "internal error") if defect == "duplicate_5xx" else (400, "duplicate names")
    natives = [function.native for function in functions]
    if defect == "description_required_5xx" and any("description" not in n for n in natives):
        return 500, "internal error while rendering tools"
    if defect == "rejects_strict" and any("strict" in native for native in natives):
        return 400, "strict is not supported"
    keywords = ('"$schema"', '"title"', '"format"', '"pattern"')
    schemas_text = json.dumps([function.schema for function in functions])
    if defect == "rejects_schema_keywords" and any(k in schemas_text for k in keywords):
        return 400, "the JSON schema uses an unsupported keyword"
    if defect == "input_examples_5xx" and any("input_examples" in n for n in natives):
        return 500, "internal error"
    seen = _history(api, body, defect)
    prompt = " ".join(seen.prompt)
    mode, chosen, parallel = _choice(api, body)
    if mode.startswith("allowed") and defect in ("rejects_allowed", "allowed_5xx"):
        return (400, "unsupported tool_choice") if defect == "rejects_allowed" else (500, "error")
    if defect == "rejects_thinking_replay" and _replays_thinking(api, body):
        return 400, "thinking blocks are not accepted in the input"
    if seen.text_beside_calls and defect == "rejects_text_beside_calls":
        return 400, "an assistant message cannot carry both content and tool calls"
    if seen.calls and not functions and defect == "history_without_tools_5xx":
        return 500, "internal error while rendering tool calls"
    if defect == "omitted_content_5xx" and _content_omitted(api, body):
        return 500, "internal error"
    if defect == "bad_arguments_5xx" and not all(map(_object_text, _replayed_arguments(api, body))):
        return 500, "internal error while parsing tool call arguments"
    form = _format_schema(api, body)
    if seen.results:
        answer = _answer(body, seen, defect)
        if isinstance(answer, tuple):
            return answer
        if form is not None and defect != "format_dropped_after_result":
            context = " ".join([prompt, *(result.text for result in seen.results)])
            answer.text = json.dumps(_instance(form, context, "", None, form))
        # A grammar fed the call tag the model repeats while reasoning about a result that
        # quotes it: the request dies, or the constrained answer comes out empty.
        if form is not None and any("<tool_call>" in result.text for result in seen.results):
            if defect == "markup_grammar_5xx":
                return 500, "grammar rejected tokens"
            if defect == "markup_empty_answer":
                answer.text = ""
            elif defect != "format_dropped_after_result":
                # The prompt asks the answer to quote the tag the notes describe.
                value = json.loads(answer.text)
                quote = "Each call is written as <tool_call>...</tool_call>."
                answer.text = json.dumps(
                    {key: quote if isinstance(item, str) else item for key, item in value.items()}
                )
        if mode not in ("auto", "required"):
            return answer
        return _chained(api, functions, seen, prompt, defect) or answer
    if form is not None and defect == "format_blocks_calls":
        text = json.dumps(_instance(form, prompt, "", None, form))
        return Turn(text=text, input_tokens=_input_tokens(body, [], defect))
    if mode == "none":
        # The empty answer arrives after reasoning, as it does live.
        text = {
            "none_empty": "<think>The weather tool would answer this.</think>",
            "none_markup": "<tool_call>get_weather</tool_call>",
        }.get(defect, "It is mild and clear today.")
        return Turn(text=text, input_tokens=_input_tokens(body, [], defect))
    if any(name not in names for name in chosen):
        return 400, "tool_choice names no declared tool"
    visible = functions[:32] if defect == "truncates_tools" else functions
    by_name = {function.name: function for function in visible}
    if any(name not in by_name for name in chosen):
        chosen = (visible[0].name,)
    if mode == "named":
        targets = [visible[0] if defect == "ignores_named" else by_name[chosen[0]]]
        if defect == "named_no_parameters_5xx" and not targets[0].schema.get("properties"):
            return 500, "internal error while building the decoding schema"
    elif mode == "allowed":
        targets = [visible[0] if defect == "ignores_allowed" else by_name[chosen[0]]]
    elif mode == "allowed_auto" and defect != "ignores_allowed":
        targets = [by_name[name] for name in chosen if name in prompt]
        if not targets:
            return Turn(text="ok", input_tokens=_input_tokens(body, [], defect))
    else:
        targets = [function for function in visible if function.name in prompt]
        wants_tool = targets or "tool" in prompt.lower()
        if mode == "auto" and not wants_tool and defect != "auto_always_calls":
            return Turn(text="ok", input_tokens=_input_tokens(body, [], defect))
        targets = targets or visible[:1]
    if not parallel and defect != "ignores_parallel_off":
        targets = targets[:1]
    if defect == "drops_third_call":
        targets = targets[:2]
    budget = body.get("max_output_tokens" if api == "responses" else "max_tokens")
    if isinstance(budget, int) and budget < 32:
        # The limit cuts the call mid-arguments, as a real engine does.
        turn = Turn(input_tokens=_input_tokens(body, [], defect), finish="length")
        partial = {} if api == "messages" else '{"text": "Rivers carve'
        turn.calls.append((_call_id(api, defect), targets[0].name, partial))
        if defect == "truncated_as_call":
            turn.finish = None
        return turn
    turn = Turn(input_tokens=_input_tokens(body, [], defect))
    for function in targets:
        arguments = _arguments(function, prompt, defect)
        name = function.name
        if defect == "truncates_names":
            name = name[:40]
        if defect == "renames_dotted":
            name = re.sub(r"[^A-Za-z0-9_-]", "_", name)
        turn.calls.append((_call_id(api, defect), name, arguments))
    if defect == "duplicate_ids":
        turn.calls = [(turn.calls[0][0], name, args) for _, name, args in turn.calls]
    if defect == "merges_parallel" and len(turn.calls) > 1:
        merged = "".join(json.dumps(args) for _, _, args in turn.calls)
        turn.calls = [(turn.calls[0][0], turn.calls[0][1], merged)]
    if defect == "markup_in_text" and turn.calls:
        turn.text = f"<tool_call>{turn.calls[0][1]}</tool_call>"
    if defect == "call_as_markup" and turn.calls:
        # The parser found no call it could parse and handed back the framing as the answer.
        turn.text, turn.calls = f"<tool_call>{turn.calls[0][1]}</tool_call>", []
    if defect == "stop_finish":
        turn.finish = "text"
    if defect == "forced_stream_as_text" and mode == "named" and body.get("stream"):
        turn.text, turn.calls = _encode(turn.calls[0][2]), []
    return turn


def _answer(body, seen: History, defect) -> Turn | tuple[int, str]:
    results = seen.results
    for result in results:
        orphan = result.call_id is None or result.call_id not in seen.calls
        if orphan and defect != "accepts_orphans":
            return 400, "a tool result names no tool call in the conversation"
    if defect == "empty_result_5xx" and any(not r.text for r in results):
        return 500, "internal error"
    answered = {result.call_id for result in results}
    if defect == "missing_result_5xx" and any(i not in answered for i in seen.calls):
        return 500, "internal error"
    used = results[:1] if defect == "drops_second_result" else results
    texts = []
    for result in used:
        text = result.text[:4000] if defect == "truncates_results" else result.text
        if defect == "escapes_unicode":
            text = text.encode("ascii", "backslashreplace").decode("ascii")
        texts.append(text)
        shown = _stripes(result.image) if result.image else None
        if shown and defect not in ("drops_result_image", "image_as_text"):
            texts.append(shown)
    answer = "No data came back." if defect == "ignores_result" else " | ".join(texts) or "Done."
    if defect == "markup_in_text":
        answer = f"{answer} <tool_call>get_weather</tool_call>"
    asked = re.search(r"end your answer with the code (\S+?)\.", " ".join(seen.after))
    if asked and defect != "drops_trailing_text":
        answer = f"{answer} {asked.group(1)}"
    return Turn(text=answer, input_tokens=_input_tokens(body, results, defect))


def _chained(
    api: str, functions: list[Function], seen: History, prompt: str, defect: str | None
) -> Turn | None:
    """A call the prompt asks for after the results, with values the results supplied."""
    called = set(seen.calls.values())
    pending = [f for f in functions if f.name in prompt and f.name not in called]
    if not pending or defect == "no_call_after_result":
        return None
    context = prompt
    if defect != "ignores_result":
        context = " ".join([prompt, *(result.text for result in seen.results)])
    turn = Turn(input_tokens=len(context.split()))
    turn.calls.append(
        (_call_id(api, defect), pending[0].name, _arguments(pending[0], context, defect))
    )
    return turn


def _format_schema(api: str, body: dict) -> dict | None:
    """The JSON Schema an output format asks the answer to match, if any."""
    if api == "chat":
        form = (body.get("response_format") or {}).get("json_schema") or {}
    elif api == "messages":
        form = (body.get("output_config") or {}).get("format") or {}
    else:
        form = (body.get("text") or {}).get("format") or {}
    schema = form.get("schema")
    return schema if isinstance(schema, dict) else None


def _replays_thinking(api: str, body: dict) -> bool:
    return api == "messages" and any(
        isinstance(block, dict) and block.get("type") == "thinking"
        for message in body.get("messages") or []
        if isinstance(message, dict) and message.get("role") == "assistant"
        for block in message.get("content") or []
    )


def _content_omitted(api: str, body: dict) -> bool:
    messages = body.get("messages") or [] if api == "chat" else []
    return any(isinstance(m, dict) and m.get("tool_calls") and "content" not in m for m in messages)


def _replayed_arguments(api: str, body: dict) -> list[Any]:
    if api == "chat":
        return [
            (call.get("function") or {}).get("arguments")
            for message in body.get("messages") or []
            if isinstance(message, dict)
            for call in message.get("tool_calls") or []
        ]
    if api == "responses" and isinstance(body.get("input"), list):
        return [
            item.get("arguments")
            for item in body["input"]
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
    return []


def _object_text(text: Any) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except (TypeError, ValueError):
        return False


def _call_id(api: str, defect: str | None) -> str:
    serial = next(_ids)
    if api == "messages":
        return f"chatcmpl-tool:{serial}" if defect == "bad_messages_id" else f"toolu_{serial:06d}"
    return f"call_{serial:06d}"


# ---------------------------------------------------------------------------- arguments


def _arguments(function: Function, prompt: str, defect: str | None) -> Any:
    description = function.description
    if defect == "truncates_description":
        description = description[:2000]
    value = _instance(function.schema, prompt, description, None, function.schema)
    if defect == "schema_violation" and isinstance(value, dict):
        required = function.schema.get("required") or []
        if required:
            value.pop(required[0], None)
    if defect == "big_int_rounded":
        value = _walk(value, lambda v: int(float(v)) if abs(v) > 2**53 else v)
    if defect == "args_as_strings":
        value = _walk(value, str)
    if defect == "empty_arguments" and value == {}:
        return ""
    if defect == "doubles_backslashes" and isinstance(value, dict):
        value = {k: v.replace("\\", "\\\\") if isinstance(v, str) else v for k, v in value.items()}
    if defect == "reorders_code_lines" and isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, str):
            lines = content.splitlines()
            value["content"] = "\n".join([*lines[1:2], *lines[:1], *lines[2:]])
    ending = {"ends_code_with_newline": "\n", "ends_code_with_blank_line": "\n\n"}.get(defect)
    if ending and isinstance(value, dict) and isinstance(value.get("content"), str):
        value["content"] += ending
    return value


def _walk(value: Any, change) -> Any:
    if isinstance(value, dict):
        return {key: _walk(item, change) for key, item in value.items()}
    if isinstance(value, list):
        return [_walk(item, change) for item in value]
    if isinstance(value, int) and not isinstance(value, bool):
        return change(value)
    return value


def _number(key: str | None, prompt: str) -> str | None:
    if key is None:
        return None
    name = re.escape(key)
    found = re.search(rf"\b{name}\W{{0,3}}(-?\d+(?:\.\d+)?)", prompt) or re.search(
        rf"(-?\d+(?:\.\d+)?)\s+{name}\b", prompt
    )
    return found.group(1) if found else None


def _instance(schema: Any, prompt: str, description: str, key: str | None, root: dict) -> Any:
    if not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            target = target[part]
        return _instance(target, prompt, description, key, root)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        options = schema["enum"]
        named = [
            v for v in options if isinstance(v, str) and re.search(rf"\b{re.escape(v)}\b", prompt)
        ]
        return named[0] if named else options[0]
    for union in ("anyOf", "oneOf"):
        if union in schema:
            return _instance(schema[union][0], prompt, description, key, root)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object" or "properties" in schema or kind is None:
        required = schema.get("required") or []
        return {
            name: _instance(sub, prompt, description, name, root)
            for name, sub in (schema.get("properties") or {}).items()
            if name in required or re.search(rf"\b{re.escape(name)}\b", prompt)
        }
    if kind == "array":
        items = schema.get("items") or {}
        count = max(schema.get("minItems", 0), 2)
        count = min(count, schema.get("maxItems", count))
        if isinstance(items, dict) and "enum" in items:
            return list(items["enum"][:count])
        return [_instance(items, prompt, description, key, root) for _ in range(count)]
    if kind in ("integer", "number"):
        found = _number(key, prompt)
        if found is not None:
            return int(float(found)) if kind == "integer" else float(found)
        low = schema.get("minimum", 1 if kind == "integer" else 0)
        return low if kind == "integer" else (low + schema.get("maximum", low + 1)) / 2
    if kind == "boolean":
        return True
    if kind == "string" and (schema.get("pattern") or schema.get("format") in STRING_FORMATS):
        found = re.search(
            schema.get("pattern", "").strip("^$") or STRING_FORMATS[schema["format"]], prompt
        )
        if found:
            return found.group(0)
    if kind == "string" and key == "content":
        block = re.search(r"^=====\n(.*?)\n=====$", prompt, re.S | re.M)
        if block:
            return block.group(1)
    if kind == "string" and key is not None:
        name = re.escape(key)
        quoted = re.search(rf'\b{name}\W{{0,3}}"([^"]*)"', prompt)
        if quoted:
            return quoted.group(1)
        told = re.search(rf"set the {name} argument to (\S+?)[.\s]", description)
        if told:
            return told.group(1)
        if key == "city":
            return next((city for city in CITIES if city in prompt), "Paris")
    return key or "value"


# ---------------------------------------------------------------------------- answering


def _payload(api: str, model: str, turn: Turn) -> dict:
    output_tokens = 5 + 5 * len(turn.calls)
    if api == "chat":
        message: dict[str, Any] = {"role": "assistant", "content": turn.text or None}
        if turn.calls:
            message["tool_calls"] = [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": _encode(args)},
                }
                for call_id, name, args in turn.calls
            ]
        finish = "tool_calls" if turn.calls and turn.finish != "text" else "stop"
        finish = "length" if turn.finish == "length" else finish
        if not turn.calls and turn.text == "":
            message["content"] = ""
        return {
            "id": "chatcmpl-tools",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": turn.input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": turn.input_tokens + output_tokens,
            },
        }
    if api == "messages":
        content: list[dict[str, Any]] = []
        if turn.thinking:
            content.append(
                {"type": "thinking", "thinking": turn.thinking, "signature": turn.signature}
            )
        if turn.text or not turn.calls:
            content.append({"type": "text", "text": turn.text})
        for call_id, name, args in turn.calls:
            content.append({"type": "tool_use", "id": call_id, "name": name, "input": args})
        stop = "tool_use" if turn.calls and turn.finish != "text" else "end_turn"
        stop = "max_tokens" if turn.finish == "length" else stop
        return {
            "id": "msg_tools",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": turn.input_tokens, "output_tokens": output_tokens},
        }
    output: list[dict[str, Any]] = []
    if turn.text or not turn.calls:
        output.append(
            {
                "id": "msg_tools",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": turn.text, "annotations": []}],
            }
        )
    for index, (call_id, name, args) in enumerate(turn.calls):
        output.append(
            {
                "id": f"fc_tools_{index}",
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": _encode(args),
                "status": "completed",
            }
        )
    cut = turn.finish == "length"
    return {
        "id": "resp_tools",
        "object": "response",
        "model": model,
        "status": "incomplete" if cut else "completed",
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if cut else None,
        "output": output,
        "usage": {
            "input_tokens": turn.input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": turn.input_tokens + output_tokens,
        },
    }


def _encode(args: Any) -> str:
    return args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)


def _thirds(text: str) -> list[str]:
    cut = max(1, len(text) // 3)
    return [text[:cut], text[cut : 2 * cut], text[2 * cut :]]


def _stream(api: str, body: dict, payload: dict, defect: str | None) -> str:
    payload = deepcopy(payload)
    if defect == "stream_args_differ":
        _shift_stream_arguments(api, payload)
    if defect == "cuts_long_stream_arguments":
        _cut_stream_arguments(api, payload, 256)
    if api == "chat":
        return _chat_stream(body, payload, defect)
    if api == "messages":
        return _message_stream(payload, defect)
    return _response_stream(payload, defect)


def _shift_stream_arguments(api: str, payload: dict) -> None:
    """Streamed arguments with one value changed, as a stream-only parser bug produces."""

    def shifted(arguments: Any) -> Any:
        value = json.loads(arguments) if isinstance(arguments, str) else arguments
        if isinstance(value, dict) and isinstance(value.get("summary"), str):
            value["summary"] += " changed"
        elif isinstance(value, dict) and value:
            first = next(iter(value))
            value[first] = [value[first], "changed"]
        return json.dumps(value) if isinstance(arguments, str) else value

    if api == "chat":
        for call in payload["choices"][0]["message"].get("tool_calls") or []:
            call["function"]["arguments"] = shifted(call["function"]["arguments"])
    elif api == "messages":
        for block in payload["content"]:
            if block["type"] == "tool_use":
                block["input"] = shifted(block["input"])
    else:
        for item in payload["output"]:
            if item["type"] == "function_call":
                item["arguments"] = shifted(item["arguments"])


def _cut_stream_arguments(api: str, payload: dict, limit: int) -> None:
    """Streamed arguments that stop early, as a parser with a fixed buffer produces."""
    if api == "chat":
        for call in payload["choices"][0]["message"].get("tool_calls") or []:
            call["function"]["arguments"] = call["function"]["arguments"][:limit]
    elif api == "messages":
        for block in payload["content"]:
            if block["type"] == "tool_use":
                block["raw"] = json.dumps(block["input"], ensure_ascii=False)[:limit]
    else:
        for item in payload["output"]:
            if item["type"] == "function_call":
                item["arguments"] = item["arguments"][:limit]


def _sse(frames: list[dict]) -> str:
    return "".join(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n" for frame in frames)


def _named(events: list[dict]) -> str:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        for event in events
    )


def _chat_stream(body: dict, payload: dict, defect: str | None) -> str:
    per_choice = [_chat_choice_frames(payload, choice, defect) for choice in payload["choices"]]
    # Choices interleave frame by frame, as an engine decoding them together sends them.
    frames = [f for group in itertools.zip_longest(*per_choice) for f in group if f is not None]
    if (body.get("stream_options") or {}).get("include_usage"):
        frames.append(
            {
                "id": payload["id"],
                "object": "chat.completion.chunk",
                "model": payload["model"],
                "choices": [],
                "usage": payload["usage"],
            }
        )
    return _sse(frames) + "data: [DONE]\n\n"


def _chat_choice_frames(payload: dict, choice: dict, defect: str | None) -> list[dict]:
    message = choice["message"]
    if defect == "n_second_as_text" and choice["index"] == 1 and message.get("tool_calls"):
        # A parser shared between choices has already consumed this choice's call.
        arguments = message["tool_calls"][0]["function"]["arguments"]
        message = {"role": "assistant", "content": arguments}
        choice = {**choice, "message": message, "finish_reason": "stop"}

    def frame(delta=None, finish=None):
        return {
            "id": payload["id"],
            "object": "chat.completion.chunk",
            "model": payload["model"],
            "choices": [{"index": choice["index"], "delta": delta, "finish_reason": finish}],
        }

    frames = [frame({"role": "assistant", "content": ""})]
    if message.get("content"):
        frames.append(frame({"content": message["content"]}))
    calls = message.get("tool_calls") or []
    for index, call in enumerate(calls):
        if defect == "merged_stream" and index:
            continue
        opener = {"index": index, "id": call["id"], "type": "function"}
        name = "" if defect == "opener_without_name" else call["function"]["name"]
        opener["function"] = {"name": name, "arguments": ""}
        frames.append(frame({"tool_calls": [opener]}))
    pieces = [_thirds(call["function"]["arguments"]) for call in calls]
    for round_index in range(3):
        for index, parts in enumerate(pieces):
            part = {
                "index": 0 if defect == "merged_stream" else index,
                "function": {"arguments": parts[round_index]},
            }
            if defect == "opener_without_name" and round_index == 0:
                part["function"]["name"] = calls[index]["function"]["name"]
            frames.append(frame({"tool_calls": [part]}))
    frames.append(frame({}, choice["finish_reason"]))
    return frames


def _message_stream(payload: dict, defect: str | None) -> str:
    start = {**payload, "content": [], "stop_reason": None}
    start["usage"] = {"input_tokens": payload["usage"]["input_tokens"], "output_tokens": 1}
    events: list[dict] = [{"type": "message_start", "message": start}]
    blocks = payload["content"]
    if defect == "merged_stream":
        uses = [block for block in blocks if block["type"] == "tool_use"]
        if len(uses) > 1:
            merged = "".join(json.dumps(block["input"]) for block in uses)
            blocks = [b for b in blocks if b["type"] != "tool_use"] + [{**uses[0], "raw": merged}]
    for index, block in enumerate(blocks):
        if block["type"] == "tool_use":
            opening = {key: value for key, value in block.items() if key != "raw"}
            opening["input"] = {}
            encoded = block.get("raw") or json.dumps(block["input"], ensure_ascii=False)
            deltas = [{"type": "input_json_delta", "partial_json": p} for p in _thirds(encoded)]
        else:
            opening = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}] if block["text"] else []
        events.append({"type": "content_block_start", "index": index, "content_block": opening})
        events += [{"type": "content_block_delta", "index": index, "delta": d} for d in deltas]
        events.append({"type": "content_block_stop", "index": index})
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": payload["stop_reason"], "stop_sequence": None},
            "usage": {"output_tokens": payload["usage"]["output_tokens"]},
        },
        {"type": "message_stop"},
    ]
    return _named(events)


def _response_stream(payload: dict, defect: str | None) -> str:
    start = {**payload, "status": "in_progress", "output": [], "usage": None}
    events: list[dict] = [
        {"type": "response.created", "response": deepcopy(start)},
        {"type": "response.in_progress", "response": deepcopy(start)},
    ]
    for index, item in enumerate(payload["output"]):
        if item["type"] == "function_call":
            opening = {**item, "arguments": "", "status": "in_progress"}
            events.append(
                {"type": "response.output_item.added", "output_index": index, "item": opening}
            )
            target = 0 if defect == "merged_stream" else index
            address = {"output_index": target, "item_id": payload["output"][target]["id"]}
            for part in _thirds(item["arguments"]):
                events.append(
                    {"type": "response.function_call_arguments.delta", **address, "delta": part}
                )
            events.append(
                {
                    "type": "response.function_call_arguments.done",
                    "output_index": index,
                    "item_id": item["id"],
                    "arguments": item["arguments"],
                }
            )
        else:
            opening = {**item, "status": "in_progress", "content": []}
            events.append(
                {"type": "response.output_item.added", "output_index": index, "item": opening}
            )
            part = item["content"][0]
            address = {"output_index": index, "item_id": item["id"], "content_index": 0}
            events.append(
                {"type": "response.content_part.added", **address, "part": {**part, "text": ""}}
            )
            if part["text"]:
                events.append(
                    {"type": "response.output_text.delta", **address, "delta": part["text"]}
                )
            events.append({"type": "response.output_text.done", **address, "text": part["text"]})
            events.append({"type": "response.content_part.done", **address, "part": deepcopy(part)})
        events.append(
            {"type": "response.output_item.done", "output_index": index, "item": deepcopy(item)}
        )
    events.append({"type": "response.completed", "response": payload})
    return _named([{**event, "sequence_number": n} for n, event in enumerate(events)])
