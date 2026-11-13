"""An Anthropic-faithful Messages backend for the fake fleet, with the defects its cases catch.

`FakeFleet(anthropic={...})` routes `/v1/messages` and `/v1/messages/count_tokens` here once the
shared checks (auth, version, model, required fields) have passed. The config keys:

- `rejects`: request shapes answered with a 400 `invalid_request_error`, named as in `shapes`.
- `hosted`: the images online, URL to bytes, which the fake serves itself so no test needs the
  network. With it, images named by a `url` source are fetched as a multimodal engine does: a
  hosted PNG is read, a missing path on a known host is a 400 (the fetch's 4xx), and a host that
  does not resolve is a 500, as vLLM reports a failed connection.
- `image_tokens`: what count_tokens counts per image, IMAGE_TOKENS unless set; a 1x1 PNG is a few
  tokens on a real tokenizer, fewer than the words between two different prompts.
- `defect`: one misbehaviour from DEFECTS, the way a real layer gets it wrong.

The model is simple and deterministic: it answers with the last marker it can see, counts when
asked to count, and continues a prefill. Token counts are words, which is all the cases compare.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from llmdv.assertions.anthropic import ERROR_TYPES, expected_error_type

MARKER = re.compile(r"llmdv[0-9a-f]{10}")
COUNTING = "1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12"
IMAGE_TOKENS = 40

DEFECTS = (
    "bad_cache_counters",  # cache counters that are not integers or do not add up
    "bad_stop_reason",  # an OpenAI finish reason in an Anthropic response
    "count_5xx_on_blocks",  # count_tokens crashes on documents and images
    "count_ignores_images",  # count_tokens leaves images out
    "count_ignores_system",  # count_tokens leaves the system prompt out
    "count_ignores_tools",  # count_tokens leaves tool definitions out
    "drops_blocks",  # document and search_result blocks never reach the model
    "drops_is_error",  # a tool_result's is_error flag never reaches the prompt
    "drops_server_tool_history",  # hosted-tool history never reaches the model
    "ignores_stop_sequence",  # generation runs past a stop sequence
    "no_stop_sequence_field",  # the stop_sequence key is missing
    "non_null_stop_sequence",  # an ordinary stop carries a non-null stop_sequence
    "openai_error_shape",  # errors in the OpenAI shape with a status-text type
    "overruns_max_tokens",  # more output than max_tokens, ending end_turn
    "renders_blocks_apart",  # a block list renders an extra separator a string does not
    "restarts_prefill",  # a prefill is answered from scratch
    "server_error_on_reject",  # an unsupported feature is a 500
    "stop_details_on_end_turn",  # refusal details on an ordinary stop
    "stop_reason_only",  # text stops at the sequence but says end_turn and null
    "stream_differs",  # the stream reports another stop reason than the message
    "stream_missing_signature",  # thinking stream omits its signature delta
    "thinking_tokens_over_output",  # thinking_tokens larger than output_tokens
    "wrong_error_type",  # a status-text error.type, the shape of an unmapped frontend error
    "url_image_5xx",  # an image named by a url source is a 500, whatever the URL
    "zero_start_usage",  # message_start reports zero input tokens
)

_STATUS_TEXT = {400: "BadRequestError", 404: "NotFoundError", 500: "InternalServerError"}
THINKING_TYPES = ("enabled", "adaptive", "disabled", "between_tools")


def _blocks(content: Any) -> list[dict]:
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _walk(body: dict):
    """Every content block in the request, nested ones included."""
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        stack = list(_blocks(message.get("content")))
        while stack:
            block = stack.pop(0)
            yield block
            stack.extend(_blocks(block.get("content")))
            source = block.get("source")
            if isinstance(source, dict):
                stack.extend(_blocks(source.get("content")))
    yield from _blocks(body.get("system"))


def shapes(body: dict) -> set[str]:
    """The request shapes the fake can be told to reject."""
    found = {
        key
        for key in (
            "metadata",
            "top_p",
            "top_k",
            "service_tier",
            "container",
            "mcp_servers",
            "inference_geo",
            "diagnostics",
            "speed",
            "cache_control",
        )
        if key in body
    }
    if body.get("max_tokens") == 0:
        found.add("max_tokens_zero")
    if len(body.get("stop_sequences") or []) > 4:
        found.add("stop_sequences_five")
    thinking = body.get("thinking") or {}
    if thinking:
        found.add("thinking_config")
    if thinking.get("type") == "between_tools":
        found.add("between_tools")
    if "display" in thinking:
        found.add("display")
    markers = 0
    for block in _walk(body):
        found.add(str(block.get("type")))
        if block.get("type") == "image" and (block.get("source") or {}).get("type") == "url":
            found.add("url_image")
        if block.get("type") == "thinking":
            found.add("thinking_block")
        if block.get("citations"):
            found.add("citations")
        control = block.get("cache_control")
        if isinstance(control, dict):
            markers += 1
            if control.get("ttl") == "1h":
                found.add("ttl_1h")
    if markers > 4:
        found.add("breakpoints")
    roles = [m.get("role") for m in body.get("messages") or [] if isinstance(m, dict)]
    if roles and roles[0] == "assistant":
        found.add("leading_assistant")
    if roles and roles[-1] == "assistant":
        found.add("prefill")
    if any(a == b == "user" for a, b in zip(roles, roles[1:], strict=False)):
        found.add("consecutive_user")
    if any(a == b == "assistant" for a, b in zip(roles, roles[1:], strict=False)):
        found.add("consecutive_assistant")
    return found


def _visible(body: dict, defect: str | None) -> list[str]:
    """The text the model reads, in order: what a renderer keeps."""
    texts: list[str] = []
    system = body.get("system")
    if isinstance(system, str):
        texts.append(system)
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
            continue
        stack = list(_blocks(content))
        while stack:
            block = stack.pop(0)
            kind = block.get("type")
            if defect == "drops_blocks" and kind in ("document", "search_result"):
                continue
            if defect == "drops_server_tool_history" and kind in (
                "server_tool_use",
                "web_search_tool_result",
            ):
                continue
            if (
                kind == "tool_result"
                and block.get("is_error") is True
                and defect != "drops_is_error"
            ):
                texts.append("Error:")
            if isinstance(block.get("text"), str):
                texts.append(block["text"])
            if kind == "server_tool_use" and isinstance(block.get("input"), dict):
                texts.extend(value for value in block["input"].values() if isinstance(value, str))
            stack.extend(_blocks(block.get("content")))
            if isinstance(block.get("content"), str):
                texts.append(block["content"])
            source = block.get("source")
            if kind == "document" and isinstance(source, dict):
                data = source.get("data")
                if source.get("type") == "text" and isinstance(data, str):
                    texts.append(data)
                elif source.get("type") == "base64" and isinstance(data, str):
                    texts.append(base64.b64decode(data).decode("latin-1"))
                stack.extend(_blocks(source.get("content")))
    texts.extend(b["text"] for b in _blocks(system) if isinstance(b.get("text"), str))
    return texts


def _invalid(body: dict) -> str | None:
    thinking = body.get("thinking")
    if thinking is not None:
        if not isinstance(thinking, dict) or thinking.get("type") not in THINKING_TYPES:
            return "thinking.type: unknown thinking type"
        if thinking["type"] == "enabled" and not (
            isinstance(thinking.get("budget_tokens"), int) and thinking["budget_tokens"] >= 1024
        ):
            return "thinking.budget_tokens: must be at least 1024"
    sequences = body.get("stop_sequences")
    if sequences is not None and (
        not isinstance(sequences, list) or not all(isinstance(s, str) for s in sequences)
    ):
        return "stop_sequences: must be a list of strings"
    return None


def error(code: int, message: str) -> tuple[int, dict]:
    return code, {"type": "error", "error": {"type": ERROR_TYPES[code], "message": message}}


def map_error(config: dict, code: int, payload: dict) -> dict:
    """Give a shared-check error the Anthropic type for its status, or the configured defect."""
    detail = payload.get("error")
    if code < 400 or not isinstance(detail, dict):
        return payload
    defect = config.get("defect")
    if defect == "openai_error_shape":
        return {"error": {**detail, "type": "Bad Request", "code": code}}
    kind = _STATUS_TEXT.get(code, "Error") if defect == "wrong_error_type" else None
    kind = kind or expected_error_type(code)
    return {"type": "error", "error": {**detail, "type": kind}}


def _words(value: Any) -> int:
    return len(json.dumps(value).split()) if value else 0


def count(config: dict, body: dict) -> tuple[int, dict]:
    defect = config.get("defect")
    if defect == "count_5xx_on_blocks" and shapes(body) & {"document", "image"}:
        return error(500, "error getting number of tokens from content list")
    total = sum(len(text.split()) for text in _visible(body, None))
    if defect != "count_ignores_images":
        per_image = config.get("image_tokens", IMAGE_TOKENS)
        total += per_image * sum(1 for b in _walk(body) if b.get("type") == "image")
    if defect != "count_ignores_tools":
        total += _words(body.get("tools"))
    if defect == "count_ignores_system":
        total -= sum(len(text.split()) for text in _visible({"system": body.get("system")}, None))
    return 200, {"input_tokens": max(total, 1)}


def _answer(body: dict, defect: str | None) -> str:
    texts = _visible(body, defect)
    joined = " ".join(texts)
    messages = body.get("messages") or []
    last = messages[-1] if messages else {}
    if "17 times 23" in joined:
        return "391"
    if "Count from 1" in joined:
        return COUNTING
    markers = MARKER.findall(joined)
    if last.get("role") == "assistant" and isinstance(last.get("content"), str):
        marker = markers[-1] if markers else "ok"
        if defect == "restarts_prefill":
            return f'```json\n{{"code": "{marker}"}}\n```'
        return f'{marker}"}}'
    return markers[-1] if markers else "ok"


def generate(config: dict, body: dict, model: str) -> dict:
    defect = config.get("defect")
    text = _answer(body, defect)
    stop_reason, stop_sequence = "end_turn", None
    sequences = [s for s in body.get("stop_sequences") or [] if s in text]
    if sequences and defect != "ignores_stop_sequence":
        first = min(sequences, key=text.index)
        text = text[: text.index(first)]
        if defect != "stop_reason_only":
            stop_reason, stop_sequence = "stop_sequence", first
    words = text.split()
    limit = body.get("max_tokens")
    output = len(words) or 1
    if isinstance(limit, int) and limit < output:
        if defect == "overruns_max_tokens":
            output = limit + 3
        else:
            text, output, stop_reason = " ".join(words[:limit]), limit, "max_tokens"
    content: list[dict] = []
    thinking = (body.get("thinking") or {}).get("type")
    if (body.get("output_config") or {}).get("effort"):
        thinking = "adaptive"
    if thinking in ("enabled", "adaptive") and limit != 0:
        content.append({"type": "thinking", "thinking": "weighing it up", "signature": "sig-fake"})
    if limit != 0:
        content.append({"type": "text", "text": text})
    if defect == "bad_stop_reason":
        stop_reason = "stop"
    rendered = sum(len(t.split()) for t in _visible(body, defect))
    if defect == "renders_blocks_apart":
        rendered += sum(1 for _ in _walk(body))
    usage: dict[str, Any] = {
        "input_tokens": max(rendered, 1),
        "output_tokens": output if limit != 0 else 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
        "output_tokens_details": {"thinking_tokens": 3 if thinking else 0},
    }
    if defect == "bad_cache_counters":
        usage["cache_creation"]["ephemeral_5m_input_tokens"] = 7
    if defect == "thinking_tokens_over_output":
        usage["output_tokens_details"]["thinking_tokens"] = usage["output_tokens"] + 5
    message = {
        "id": "msg_fake_anthropic",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence,
        "stop_details": None,
        "usage": usage,
    }
    if defect == "no_stop_sequence_field":
        del message["stop_sequence"]
    if defect == "non_null_stop_sequence" and stop_reason != "stop_sequence":
        message["stop_sequence"] = 123
    if defect == "stop_details_on_end_turn":
        message["stop_details"] = {"type": "refusal", "category": None, "explanation": None}
    return message


def stream(config: dict, message: dict) -> str:
    defect = config.get("defect")
    usage = message["usage"]
    start = {
        **message,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 0 if defect == "zero_start_usage" else usage["input_tokens"],
            "output_tokens": 1,
        },
    }
    events: list[dict] = [{"type": "message_start", "message": start}]
    for index, block in enumerate(message["content"]):
        if block["type"] == "thinking":
            events.append(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                }
            )
            events.append(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "thinking_delta", "thinking": block["thinking"]},
                }
            )
            if defect != "stream_missing_signature":
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "signature_delta", "signature": block["signature"]},
                    }
                )
        else:
            half = len(block["text"]) // 2
            events.append(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                }
            )
            events += [
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": part},
                }
                for part in (block["text"][:half], block["text"][half:])
            ]
        events.append({"type": "content_block_stop", "index": index})
    reason = "max_tokens" if defect == "stream_differs" else message["stop_reason"]
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": reason, "stop_sequence": message.get("stop_sequence")},
            "usage": {"output_tokens": usage["output_tokens"]},
        },
        {"type": "message_stop"},
    ]
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def host_of(url: str) -> str:
    return re.sub(r"^[a-z]+://([^/:?#]+).*$", r"\1", url)


def fetch(hosted: dict[str, bytes], url: str) -> tuple[int, dict] | None:
    """Fetch one image URL from the fake's own hosted images; an error reply when it fails."""
    image = hosted.get(url)
    if image is not None:
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            return error(400, f"{url} is not an image")
        return None
    host = host_of(url)
    if any(host_of(known) == host for known in hosted):
        return error(400, f"fetching {url} failed with 404 Not Found")
    return error(500, f"Cannot connect to host {host}:443 [Name or service not known]")


def reply(config: dict, path: str, body: dict, model: str) -> tuple[int, dict | str]:
    """(status, JSON payload) or (200, SSE text) for a request the shared checks accepted."""
    problem = _invalid(body)
    if problem:
        return error(400, problem)
    rejected = shapes(body) & set(config.get("rejects", ()))
    if rejected:
        if config.get("defect") == "server_error_on_reject":
            return error(500, f"unsupported: {sorted(rejected)}")
        return error(400, f"unsupported: {sorted(rejected)}")
    if path.endswith("/count_tokens"):
        return count(config, body)
    urls = [
        str(block["source"].get("url"))
        for block in _walk(body)
        if block.get("type") == "image" and (block.get("source") or {}).get("type") == "url"
    ]
    if urls and config.get("defect") == "url_image_5xx":
        return error(500, "image url source failed")
    if "hosted" in config:
        for url in urls:
            problem = fetch(config["hosted"], url)
            if problem:
                return problem
    message = generate(config, body, model)
    if body.get("stream"):
        return 200, stream(config, message)
    return 200, message
