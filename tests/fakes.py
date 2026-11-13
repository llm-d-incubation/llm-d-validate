"""A fake fleet: enough of vLLM's OpenAI surface to run the suites without a cluster.

A real server on a real socket, not a mock. That matters here because what is being tested is
HTTP behaviour: statuses, headers, error envelopes and route presence. A mock would assert
against llmdv's own assumptions rather than against the wire.

Defaults reproduce a measured fleet's behaviour: inline <think> reasoning with a
null reasoning_content, 404 with an OpenAI envelope for an unknown model, render endpoints
absent, and no x-request-id echo.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

from llmdv.profile.schema import MESSAGES_EFFORTS
from tests import transport_fakes
from tests.fake_anthropic import map_error as anthropic_error
from tests.fake_anthropic import reply as anthropic_reply

MODEL = "fake/Model-1B"


def reasoning_usage_payload(surface: str, count) -> dict:
    """Native counters, including malformed values a proxy might pass through."""
    if surface == "responses":
        return {
            "usage": {
                "input_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": count},
            }
        }
    return {
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": count},
        }
    }


def prompt_words(body: dict, defect: str | None = None) -> int:
    """A synthetic prompt size: the content a layer renders, less whatever a defect drops."""
    kept = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if defect == "drops_mid_system" and message.get("role") == "system":
            continue
        content = message.get("content")
        if defect == "drops_thinking" and isinstance(content, list):
            content = [b for b in content if b.get("type") != "thinking"]
        kept.append(content)
    system = body.get("system")
    if defect == "drops_later_system_blocks" and isinstance(system, list):
        system = system[:1]
    return len(json.dumps([system, kept]).split())


def mid_system_answer(body: dict, defect: str | None) -> str | None:
    """Follow a system entry placed inside the conversation, unless a defect loses it."""
    if defect in ("drops_mid_system", "ignores_mid_system"):
        return None
    notes = [
        m["content"]
        for m in body.get("messages") or []
        if isinstance(m, dict) and m.get("role") == "system" and isinstance(m.get("content"), str)
    ]
    return notes[-1] if notes else None


THOUGHT = "Let me work this out step by step before I give the answer"
# Reasoning that reached the answer channel, each the way a broken parser or proxy delivers it.
LEAKS = {
    "glued": "<think>{thought}</think>{answer}",  # a whole block, where a field is declared
    "unopened": "{thought}</think>{answer}",  # the template opened the tag in the prompt
    "untagged": "{thought}. The answer is {answer}.",  # reasoning text without markup
    "swallowed": "",  # the answer went to the reasoning channel
}


def leaked_answer(answer: str, leak: str | None = None) -> str:
    return answer if leak is None else LEAKS[leak].format(thought=THOUGHT, answer=answer)


def spoken_answer(body: dict) -> str:
    """A healthy model's answer: the product for the arithmetic prompt, else the one word."""
    line = transport_fakes.requested_line(body)
    if line is not None:
        return line
    asked = json.dumps([body.get(key) for key in ("messages", "input", "prompt")])
    return "391" if "17 times 23" in asked else "ok"


# A question about the code word, how a context states one (given, or chosen by the model in
# a replayed assistant turn), and the request that makes the model's own choice a fact.
CODE_WORD_QUESTIONS = ("What is the code word?", "Which code word did you choose?")
CODE_WORD_STATED = re.compile(r"(?:The code word is|I chose) (\w+)\.")
CODE_WORD_CHOICE = "Choose a code word"


def _supported(context):
    """The context less assistant turns that state a code word nothing asked the model to
    choose: a healthy model declines to repeat a claim the conversation never established."""
    if not isinstance(context, list):
        return context
    kept, asked = [], False
    for item in context:
        said = json.dumps(item)
        if isinstance(item, dict) and item.get("role") == "user" and CODE_WORD_CHOICE in said:
            asked = True
        unprompted = isinstance(item, dict) and item.get("role") == "assistant" and not asked
        if not (unprompted and CODE_WORD_STATED.search(said)):
            kept.append(item)
    return kept


def code_word_answer(*contexts) -> str | None:
    """The code word a healthy model reads from its context, when the request asks for it."""
    texts = [json.dumps(_supported(context)) for context in contexts]
    if not any(question in texts[-1] for question in CODE_WORD_QUESTIONS):
        return None
    for text in reversed(texts):
        stated = CODE_WORD_STATED.findall(text)
        if stated:
            return stated[0]
    return "unknown"


def response_input_view(body: dict, defect: str | None) -> tuple:
    """The instructions and input a Responses layer passes to the model, less what a defect
    loses: a role, replayed assistant items, all but the first text part, or instructions."""
    instructions = None if defect == "drops_instructions" else body.get("instructions")
    items = body.get("input")
    if not isinstance(items, list):
        return instructions, items
    kept = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if defect == f"drops_role:{item.get('role')}":
            continue
        if (
            defect == "instructions_replace_system"
            and instructions
            and item.get("role")
            in (
                "system",
                "developer",
            )
        ):
            continue
        if defect == "drops_assistant" and item.get("role") == "assistant":
            continue
        content = item.get("content")
        if defect == "first_part_only" and isinstance(content, list):
            item = {**item, "content": content[:1]}
        if defect == "drops_output_parts" and isinstance(item.get("output"), list):
            item = {**item, "output": ""}
        kept.append(item)
    return instructions, kept


REPEAT = "Repeat your previous answer"


def replayed_answer(given) -> str | None:
    """The last assistant text in the input, when the request asks to repeat it."""
    if not isinstance(given, list) or not given or REPEAT not in json.dumps(given[-1]):
        return None
    for item in reversed(given[:-1]):
        if isinstance(item, dict) and item.get("role") == "assistant":
            content = item.get("content")
            if isinstance(content, str):
                return content
            return "".join(p.get("text", "") for p in content if p.get("type") == "output_text")
    return "unknown"


def _has_null(value) -> bool:
    if isinstance(value, dict):
        return any(v is None or _has_null(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_null(v) for v in value)
    return False


def response_input_refusal(body: dict, defect: str | None) -> str | None:
    """Why a defective layer refuses a valid input list, or None."""
    items = body.get("input") if isinstance(body.get("input"), list) else []
    if defect == "rejects_null_fields" and _has_null(items):
        return "input: 3 validation errors"
    if defect == "rejects_reasoning" and any(i.get("type") == "reasoning" for i in items):
        return "reasoning items are not supported"
    if defect == "rejects_phase" and any("phase" in i for i in items):
        return "unknown field phase"
    if defect == "rejects_encrypted" and any(i.get("encrypted_content") for i in items):
        return "encrypted_content could not be verified"
    if defect == "rejects_compaction" and any(i.get("type") == "compaction" for i in items):
        return "unknown item type compaction"
    if defect == "rejects_output_parts" and any(isinstance(i.get("output"), list) for i in items):
        return "function_call_output.output: Input should be a valid string"
    return None


FEATURE_KEYS = (
    "background", "context_management", "conversation", "include", "max_tool_calls",
    "moderation", "parallel_tool_calls", "prompt", "prompt_cache_key", "prompt_cache_options",
    "prompt_cache_retention", "reasoning", "safety_identifier", "service_tier", "text",
    "top_p", "truncation", "user", "stream_options", "access_programs",
)  # fmt: skip
TOOL_CALL_ITEMS = {
    "web_search_preview": "web_search_call",
    "computer_use_preview": "computer_call",
    "mcp": "mcp_call",
    "programmatic_tool_calling": "program",
}


def requested_features(body: dict) -> set[str]:
    """The optional Responses fields, built-in tools and input parts a request carries."""
    tools = body.get("tools") or []
    found = {t.get("type") for t in tools if isinstance(t, dict) and t.get("type") != "function"}
    found |= {key for key in FEATURE_KEYS if key in body}
    if body.get("temperature") not in (None, 0):
        found.add("temperature")
    items = body.get("input") if isinstance(body.get("input"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("item_reference", "additional_tools"):
            found.add(item["type"])
        parts = item.get("content") if isinstance(item.get("content"), list) else []
        found |= {p.get("type") for p in parts if p.get("type") in ("input_image", "input_file")}
    return found


def feature_payload(body: dict, model: str, mode: str) -> dict:
    """A generation by a fleet that runs no built-in tool, in one of the accepting modes:
    `ignore` drops every feature, `echo` honors and echoes them, `wrong_echo` echoes other
    values, `drops_sampling` echoes the default 1.0 for top_p and temperature, as an engine that
    never applied them, and `phantom_call` reports a built-in tool call that never ran.

    `echo` reports the sampling it applied, as vLLM does: at temperature 0 decoding is greedy,
    so top_p is 1.0 whatever was asked."""
    payload = response_body(model)
    # Full reasoning without a summary or encrypted content, as an engine that ignores both.
    reasoning = {"type": "reasoning", "id": "rs_fake", "summary": [], "encrypted_content": None}
    reasoning["content"] = [{"type": "reasoning_text", "text": "thinking"}]
    payload["output"].insert(0, reasoning)
    files = [
        part.get("file_data", "").split(",", 1)[-1]
        for item in body.get("input") or []
        if isinstance(item, dict) and isinstance(item.get("content"), list)
        for part in item["content"]
        if part.get("type") == "input_file"
    ]
    if mode == "echo":
        for key in FEATURE_KEYS + ("temperature",):
            if key in body:
                payload[key] = deepcopy(body[key])
        if "top_p" in body and body.get("temperature") == 0:
            payload["top_p"] = 1.0
        if (body.get("reasoning") or {}).get("summary"):
            reasoning["summary"] = [{"type": "summary_text", "text": "thinking"}]
        if "reasoning.encrypted_content" in (body.get("include") or []):
            reasoning["encrypted_content"] = "opaque"
        decoded = [base64.b64decode(data).decode() for data in files]
        word = code_word_answer(*decoded, body.get("input")) if decoded else None
        if word is not None:
            payload["output"][-1]["content"][0]["text"] = word
    elif mode == "wrong_echo":
        for key in FEATURE_KEYS + ("temperature",):
            if key in body:
                payload[key] = "changed"
    elif mode == "drops_sampling":
        payload.update(top_p=1.0, temperature=1.0)
    elif mode == "phantom_call":
        for tool in body.get("tools") or []:
            kind = tool.get("type")
            if kind != "function":
                call = TOOL_CALL_ITEMS.get(kind, f"{kind}_call")
                payload["output"].insert(0, {"type": call, "id": "tc_fake", "status": "completed"})
    return payload


LONG_COUNT = "Count from 1 to 200"


def response_shape(payload: dict, body: dict, defect: str | None) -> None:
    """Complete the typed Response fields, stop a long count at a small max_output_tokens,
    then apply one shape defect."""
    payload.update(created_at=0, parallel_tool_calls=True, tool_choice="auto", tools=[])
    for item in payload["output"]:
        if item.get("type") == "message":
            item.setdefault("phase", None)  # vLLM returns these nulls, and clients replay them
            for part in item.get("content") or []:
                part.setdefault("logprobs", None)
    if payload.get("status") == "completed" and defect != "no_completed_at":
        payload["completed_at"] = 1
    usage = payload["usage"]
    usage.setdefault("input_tokens_details", {"cached_tokens": 0})
    usage.setdefault("output_tokens_details", {"reasoning_tokens": 0})
    cap = body.get("max_output_tokens")
    if LONG_COUNT in json.dumps(body.get("input")) and isinstance(cap, int) and cap <= 16:
        payload["output"][-1]["content"][0]["text"] = "1\n2\n3"
        payload["output"][-1]["status"] = "incomplete"
        payload["status"] = "incomplete"
        payload["incomplete_details"] = {"reason": "max_output_tokens"}
        payload["usage"].update(
            output_tokens=cap, total_tokens=payload["usage"]["input_tokens"] + cap
        )
        if defect == "limit_completed":
            payload.update(status="completed", incomplete_details=None)
        elif defect == "limit_no_reason":
            payload["incomplete_details"] = None
        elif defect == "limit_over_cap":
            payload["usage"].update(output_tokens=cap * 2, total_tokens=3 + cap * 2)
    if defect == "missing_fields":
        for key in ("tools", "created_at"):
            payload.pop(key, None)
    elif defect == "duplicate_ids":
        item = {"type": "reasoning", "id": payload["output"][-1]["id"], "summary": []}
        payload["output"].insert(0, {**item, "content": [{"type": "reasoning_text", "text": "x"}]})
    elif defect == "no_annotations":
        del payload["output"][-1]["content"][0]["annotations"]
    elif defect == "no_usage_details":
        payload["usage"].pop("input_tokens_details", None)


def thinking_blocks(defect=None, leak=None):
    """Native thinking, with independent wire defects for the option probes."""
    thought = {"type": "thinking", "thinking": "17*20+17*3", "signature": "sig-fake"}
    if defect == "signature":
        del thought["signature"]
    if defect == "text":
        thought["thinking"] = 42
    blocks = [thought, {"type": "text", "text": leaked_answer("391", leak)}]
    if defect == "ignored":
        return blocks[1:]
    if defect == "order":
        blocks.reverse()
    return blocks


def chat_reasoning_payload(carrier: str, leak: str | None = None) -> dict:
    """Also models a proxy dropping reasoning or renaming its field."""
    message = {"role": "assistant", "content": leaked_answer("391", leak)}
    if carrier == "inline":
        message["content"] = f"<think>17*20+17*3=391</think>{message['content']}"
    elif carrier != "missing":
        message[carrier] = "17*20+17*3=391"
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


def message_tool_payload(body, model, defect):
    results = [
        part
        for message in body["messages"]
        if message.get("role") == "user"
        for part in (message.get("content") if isinstance(message.get("content"), list) else [])
        if part.get("type") == "tool_result"
    ]
    if results:
        previous = body["messages"][-2]["content"]
        signed = any(p.get("signature") == "sig-fake" for p in previous)
        ok = results[0].get("tool_use_id") == "toolu_fake" and signed and defect != "ignores_result"
        answer = results[0]["content"] if ok else "wrong result"
        if defect == "prose_result":
            answer = f"The result is {answer}."
        content = [{"type": "text", "text": answer}]
        reason = "end_turn"
    else:
        marker = body["tools"][0]["input_schema"]["properties"]["marker"]["enum"][0]
        value = {"marker": "wrong" if defect == "schema" else marker}
        if defect == "argument_type":
            value = json.dumps(value)
        content = [
            {"type": "thinking", "thinking": "use the tool", "signature": "sig-fake"},
            {"type": "text", "text": "Calling the tool."},
            {
                "type": "tool_use",
                "id": "" if defect == "id" else "toolu_fake",
                "name": "wrong" if defect == "name" else "llmdv_echo",
                "input": value,
            },
        ]
        reason = {"abort": "abort", "end_turn": "end_turn"}.get(defect, "tool_use")
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": reason,
        "usage": {"input_tokens": 3, "output_tokens": 10},
    }


def message_parallel_payload(body, model, defect):
    """One call per offered tool in a single turn, then an answer built from every result."""
    results = [
        part
        for part in (body["messages"][-1].get("content") or [])
        if isinstance(part, dict) and part.get("type") == "tool_result"
    ]
    if results:
        if defect == "ignores_second_result":
            results = results[:1]
        content = [{"type": "text", "text": " ".join(part["content"] for part in results)}]
        reason = "end_turn"
    else:
        tools = body["tools"][:1] if defect == "single_call" else body["tools"]
        content = [{"type": "thinking", "thinking": "use the tools", "signature": "sig-fake"}]
        for index, tool in enumerate(tools):
            marker = tool["input_schema"]["properties"]["marker"]["enum"][0]
            content.append(
                {
                    "type": "tool_use",
                    "id": "toolu_fake" if defect == "duplicate_id" else f"toolu_fake_{index}",
                    "name": tool["name"],
                    "input": {"marker": marker},
                }
            )
        reason = "tool_use"
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": reason,
        "usage": {"input_tokens": 3, "output_tokens": 10},
    }


def message_tool_events(payload, defect):
    events = [{"type": "message_start", "message": {**payload, "content": [], "stop_reason": None}}]
    for index, block in enumerate(payload["content"]):
        kind = block["type"]
        if kind == "tool_use":
            opening = {**block, "input": {}}
            if defect == "nonempty_start":
                opening["input"] = {"marker": "stale"}
            encoded = json.dumps(block["input"])
            parts = [encoded[:3], encoded[3:-2], encoded[-2:]]
            if defect == "dropped_fragment":
                parts.pop(1)
            deltas = [{"type": "input_json_delta", "partial_json": part} for part in parts]
        elif kind == "thinking":
            opening = {"type": "thinking", "thinking": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": block["signature"]},
            ]
        else:
            opening = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}]
        events.append({"type": "content_block_start", "index": index, "content_block": opening})
        for delta in deltas:
            events.append(
                {
                    "type": "content_block_delta",
                    "index": 9 if defect == "index" else index,
                    "delta": delta,
                }
            )
        events.append({"type": "content_block_stop", "index": index})
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": payload["stop_reason"]},
            "usage": {"output_tokens": 10},
        },
        {"type": "message_stop"},
    ]
    return events


# Small enough that the overflow case builds a modest body, large enough to be realistic.
MAX_MODEL_LEN = 256

ABSENT_PATHS = frozenset(
    {
        "/v1/chat/completions/render",
        "/v1/completions/render",
        "/v1/messages/render",
        "/v1/responses/render",
    }
)

COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
POST_PATHS = (
    "/v1/chat/completions", "/v1/completions", "/v1/messages", "/v1/responses", COUNT_TOKENS_PATH,
)  # fmt: skip


def chat_choice_payload(body, model, defect):
    count = 1 if defect == "ignored_n" else body.get("n", 1)
    # Sampled choices phrase the same answer differently.
    choices = [
        {
            "index": i,
            "message": {"role": "assistant", "content": ("ok", "Ok.")[i % 2]},
            "finish_reason": "stop",
        }
        for i in range(count)
    ]
    if defect == "leaked_reasoning":
        choices[-1]["message"]["content"] = leaked_answer("ok", "untagged")
    if defect == "duplicate_index":
        choices[-1]["index"] = 0
    if defect == "abort":
        choices[-1]["finish_reason"] = "abort"
    if body.get("logprobs") and defect != "ignored_logprobs":
        for choice in choices:
            choice["logprobs"] = {
                "content": [
                    {
                        "token": "ok",
                        "logprob": -0.1,
                        "top_logprobs": [{"token": "ok", "logprob": -0.1}],
                    }
                ]
            }
    usage = {"prompt_tokens": 3, "completion_tokens": count, "total_tokens": 3 + count}
    payload = {
        "id": "chat-fake",
        "model": model,
        "object": "chat.completion",
        "choices": choices,
        "usage": usage,
    }

    def frame(values, **extra):
        return {
            "id": "chat-fake",
            "model": model,
            "object": "chat.completion.chunk",
            "choices": values,
            **extra,
        }

    frames = [
        frame(
            [
                {"index": c["index"], "delta": {"role": "assistant"}, "finish_reason": None}
                for c in choices
            ]
        )
    ]
    for c in reversed(choices):
        frames.append(
            frame(
                [
                    {
                        "index": c["index"],
                        "delta": {"content": c["message"]["content"]},
                        "finish_reason": None,
                    }
                ]
            )
        )
        if defect != "missing_finish" or c["index"] == 0:
            frames.append(
                frame([{"index": c["index"], "delta": {}, "finish_reason": c["finish_reason"]}])
            )
    if defect == "late_delta":
        frames.append(frame([{"index": 0, "delta": {"content": "late"}, "finish_reason": None}]))
    if defect != "missing_usage":
        frames.append(frame([], usage=usage))
    stream = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
    return payload, stream


def chat_tool_payload(body, model, defect, forced_finish="tool_calls"):
    message = {"role": "assistant", "content": None}
    result = next((m for m in body["messages"] if m.get("role") == "tool"), None)
    if result:
        correct_id = result.get("tool_call_id") == "call_fake"
        message["content"] = (
            result["content"] if correct_id and defect != "ignores_result" else "wrong result"
        )
        if defect == "prose_result":
            message["content"] = f"The result is {message['content']}."
        finish = "stop"
    else:
        schema = body["tools"][0]["function"]["parameters"]
        marker = schema["properties"]["marker"]["enum"][0]
        arguments = json.dumps({"marker": "wrong" if defect == "schema" else marker})
        if defect == "json":
            arguments = arguments[:-1]
        if defect == "argument_type":
            arguments = {"marker": marker}
        message["tool_calls"] = [
            {
                "id": "" if defect == "id" else "call_fake",
                "type": "function",
                "function": {
                    "name": "wrong" if defect == "name" else "llmdv_echo",
                    "arguments": arguments,
                },
            }
        ]
        finish = "abort" if defect == "abort" else forced_finish
        if defect == "ignored_choice":
            message = {"role": "assistant", "content": "ok"}
            finish = "stop"
    return {
        "id": "chat-fake",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def chat_tool_stream(payload, defect):
    choice = payload["choices"][0]
    message = choice["message"]
    call = (
        message.get("tool_calls")
        or [{"id": "call_fake", "function": {"name": "llmdv_echo", "arguments": "{}"}}]
    )[0]

    def frame(delta, finish=None, usage=False):
        return {
            "id": payload["id"],
            "object": "chat.completion.chunk",
            "model": payload["model"],
            "choices": [] if usage else [{"index": 0, "delta": delta, "finish_reason": finish}],
            **({"usage": payload["usage"]} if usage else {}),
        }

    frames = [
        frame({"role": "assistant"}),
        frame(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["function"]["name"][:3], "arguments": ""},
                    }
                ]
            }
        ),
    ]
    arguments = call["function"]["arguments"]
    fragments = (
        [arguments]
        if isinstance(arguments, dict)
        else [arguments[:3], arguments[3:-2], arguments[-2:]]
    )
    if defect == "dropped_fragment":
        fragments.pop(1)
    for position, fragment in enumerate(fragments):
        part = {"index": 4 if defect == "index" else 0, "function": {"arguments": fragment}}
        if position == 0:
            part["function"]["name"] = call["function"]["name"][3:]
        if defect == "id_changed":
            part["id"] = "call_other"
        frames.append(frame({"tool_calls": [part]}))
    frames += [frame({}, choice["finish_reason"]), frame({}, usage=True)]
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"


def structured_answer(body: dict, path: str, defect: str | None) -> str | None:
    """Recognize each API's schema field independently of the production adapters."""
    if path == "/v1/chat/completions":
        output_format = body.get("response_format", {})
        if output_format.get("type") == "json_object":
            return "ok" if defect else '{"count": 1}'
        if output_format.get("type") != "json_schema":
            return None
        config = output_format.get("json_schema", {})
        if config.get("strict") is not True:
            return None
    elif path == "/v1/responses":
        config = body.get("text", {}).get("format", {})
        if config.get("type") == "json_object":
            return "ok" if defect else '{"count": 1}'
        if config.get("type") != "json_schema" or config.get("strict") is not True:
            return None
    elif path == "/v1/messages":
        config = body.get("output_config", {}).get("format", {})
        if config.get("type") != "json_schema":
            return None
    else:
        return None
    schema = config.get("schema", {})
    if "marker" not in schema.get("properties", {}):
        return json.dumps(schema_example(schema))
    marker = schema.get("properties", {}).get("marker", {}).get("enum", [None])[0]
    value = {"marker": marker, "count": 1}
    if defect == "ignores_schema":
        value["marker"] = "fixed-answer"
    elif defect == "wrong_type":
        value["count"] = "1"
    elif defect == "missing_field":
        del value["count"]
    elif defect == "extra_field":
        value["unexpected"] = True
    elif defect == "invalid_json":
        return json.dumps(value)[:-1]
    elif defect == "reasoning_only":
        return "<think>still thinking</think>"
    return json.dumps(value)


def schema_example(schema: dict) -> dict:
    """A client's own schema answered with the first allowed value of each required field."""
    properties = schema.get("properties", {})
    return {
        name: (properties.get(name, {}).get("enum") or ["ok"])[0]
        for name in schema.get("required", [])
    }


def reasoning_off_payload(body: dict, model: str, defect: str) -> dict:
    """A Chat schema answer from a stack that reasons though the client turned reasoning off.

    `budget_spent` reasons before the answer, so a small budget ends inside the reasoning.
    `reasoning_in_content` puts the reasoning text in front of the answer, in the content.
    """
    answer = structured_answer(body, "/v1/chat/completions", None)
    budget = body.get("max_tokens") or 0
    content, finish, used = f"<think>weighing it up</think>{answer}", "stop", 9
    if defect == "budget_spent" and budget < 64:
        content, finish, used = "<think>weighing it up", "length", budget
    elif defect == "reasoning_in_content":
        content = f"Weighing it up, as requested.{answer}"
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, "reasoning_content": None},
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": used, "total_tokens": 3 + used},
    }


def effort_tool_payload(body: dict, model: str, defect: str) -> dict | None:
    """An automatic Chat tool call gone wrong at one reasoning setting, or None to serve it.

    `markup_at_effort` writes the call as raw markup in the content when an effort is set.
    `runaway_default` reasons until the budget runs out when no effort is set.
    """
    if body.get("tool_choice") != "auto" or not body.get("tools"):
        return None
    effort = body.get("reasoning_effort")
    budget = body.get("max_tokens") or 0
    name = body["tools"][0]["function"]["name"]
    if defect == "markup_at_effort" and effort is not None:
        content, finish, used = f'<tool_call>{name}{{"marker": "x"}}</tool_call>', "stop", 12
    elif defect == "runaway_default" and effort is None:
        content, finish, used = "<think>checking the request again", "length", budget
    else:
        return None
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, "reasoning_content": None},
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": used, "total_tokens": 3 + used},
    }


def quiet_effort_payload(
    body: dict, model: str, quiet: tuple[str, ...], defect: str | None
) -> dict | None:
    """A Chat answer without reasoning at a quiet effort, or None to reason as usual.

    `reasons` ignores a quiet effort sent top level. `kwargs_ignored` ignores one sent as a
    template kwarg.
    """
    top = "reasoning_effort" in body
    effort = body.get(
        "reasoning_effort", (body.get("chat_template_kwargs") or {}).get("reasoning_effort")
    )
    ignored = (defect == "reasons" and top) or (defect == "kwargs_ignored" and not top)
    if effort not in quiet or ignored:
        return None
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": spoken_answer(body),
                    "reasoning_content": None,
                },
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


# What a GLM-5.3-like template renders for each requested effort under a deployment effort map
# that sends none, minimal and medium to low and xhigh to max; `unset` is the template default.
# DeepSeek-like `none` renders no marker at all, which `effort_render` can ask for with None.
RENDERED_EFFORTS = {
    "unset": "Max",
    "none": "Low",
    "minimal": "Low",
    "low": "Low",
    "medium": "Low",
    "high": "High",
    "xhigh": "Max",
    "max": "Max",
}


def requested_effort(path: str, body: dict) -> str:
    """The effort a request carries in its own API's field, or `unset`."""
    if path == "/v1/responses":
        effort = (body.get("reasoning") or {}).get("effort")
    elif path == "/v1/messages":
        effort = (body.get("output_config") or {}).get("effort")
    else:
        effort = body.get("reasoning_effort")
    return effort or "unset"


def effort_render_payload(
    path: str, body: dict, model: str, efforts: dict, defect: str | None
) -> dict:
    """A one-token answer whose `prompt_logprobs` spell the prompt the template rendered.

    The template writes a system line `Reasoning Effort: <Level>` from `efforts`, or no system
    line where the level is None. `wrong_level:<effort>` renders Low for that one effort,
    `drops_prompt_logprobs` answers without them, and `no_marker` renders no effort line.
    """
    effort = requested_effort(path, body)
    level = efforts.get(effort)
    if defect == f"wrong_level:{effort}":
        level = "Low" if level != "Low" else "High"
    if defect == "no_marker":
        level = None
    system = "" if level is None else f"<|system|>Reasoning Effort: {level}"
    rendered = f"[gMASK]<sop>{system}<|user|>hi<|assistant|><think>"
    tokens = re.findall(r"<\|?[^>]*>|\w+|[^\w<]+", rendered)
    entries: list = [None] + [
        {str(500 + index): {"logprob": -0.5, "rank": 1, "decoded_token": token}}
        for index, token in enumerate(tokens[1:])
    ]
    if path == "/v1/responses":
        payload = response_body(model)
    elif path == "/v1/messages":
        payload = {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": len(tokens), "output_tokens": 1},
        }
    else:
        payload = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
            "usage": {"prompt_tokens": len(tokens), "completion_tokens": 1, "total_tokens": 0},
        }
    if defect != "drops_prompt_logprobs":
        payload["prompt_logprobs"] = entries
    return payload


def chat_text_stream(payload: dict) -> str:
    """A non-streamed Chat text payload as a stream: role, content, finish, then usage."""
    choice = payload["choices"][0]

    def frame(choices, usage=None):
        chunk = {
            "id": payload["id"],
            "object": "chat.completion.chunk",
            "model": payload["model"],
            "choices": choices,
        }
        return chunk if usage is None else {**chunk, "usage": usage}

    frames = [
        frame([{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]),
        frame(
            [
                {
                    "index": 0,
                    "delta": {"content": choice["message"]["content"]},
                    "finish_reason": None,
                }
            ]
        ),
        frame([{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]),
        frame([], payload["usage"]),
    ]
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"


def response_option_payload(payload, body, defect):
    if defect == "ignored":
        return
    for key in ("metadata", "reasoning"):
        if key in body:
            payload[key] = body[key]
    if "message.output_text.logprobs" in body.get("include", []):
        entry = {
            "token": "ok",
            "logprob": -0.1,
            "top_logprobs": [{"token": "ok", "logprob": -0.1}, {"token": "yes", "logprob": -2.5}],
        }
        if defect == "invalid_logprob":
            entry["logprob"] = 1.0
        payload["output"][0]["content"][0]["logprobs"] = [entry]


def response_body(model: str = MODEL) -> dict:
    """A completed Responses generation, including its native item and usage shapes."""
    return {
        "id": "resp_fake",
        "object": "response",
        "model": model,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "ok", "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    }


def reasoning_only_response() -> dict:
    """Overrides for an incomplete Response that spent its budget without an answer."""
    return {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            {
                "id": "rs_fake",
                "type": "reasoning",
                "status": "incomplete",
                "summary": [],
                "content": [{"type": "reasoning_text", "text": "still thinking"}],
            }
        ],
    }


def response_events(model: str = MODEL, *, response: dict | None = None) -> list[dict]:
    """A Responses text/reasoning stream, with independently copied terminal snapshots."""
    final = deepcopy(response if response is not None else response_body(model))
    start = {
        **final,
        "status": "in_progress",
        "output": [],
        "usage": None,
        "error": None,
        "incomplete_details": None,
    }
    events = [
        {"type": "response.created", "response": deepcopy(start)},
        {"type": "response.in_progress", "response": deepcopy(start)},
    ]
    output = final.get("output")
    for index, item in enumerate(output if isinstance(output, list) else []):
        opening = {**item, "status": "in_progress", "content": [], "summary": []}
        events.append(
            {"type": "response.output_item.added", "output_index": index, "item": opening}
        )
        for bucket in ("content", "summary"):
            for part_index, part in enumerate(item.get(bucket, [])):
                kind = part["type"]
                part_event = {
                    "reasoning_text": "reasoning_part",
                    "summary_text": "reasoning_summary_part",
                }.get(kind, "content_part")
                text_event = {
                    "output_text": "output_text",
                    "reasoning_text": "reasoning_text",
                    "summary_text": "reasoning_summary_text",
                    "refusal": "refusal",
                }[kind]
                text_key = "refusal" if kind == "refusal" else "text"
                address = {
                    "output_index": index,
                    "item_id": item["id"],
                    f"{bucket}_index": part_index,
                }
                events.append(
                    {
                        "type": f"response.{part_event}.added",
                        **address,
                        "part": {**part, text_key: ""},
                    }
                )
                for fragment in part[text_key]:
                    events.append(
                        {"type": f"response.{text_event}.delta", **address, "delta": fragment}
                    )
                    # Requested logprobs stream with the deltas, one token per delta while
                    # the part has them, so the deltas carry exactly the part's tokens.
                    if isinstance(part.get("logprobs"), list):
                        sent = sum(len(e.get("logprobs", [])) for e in events if e is not None)
                        events[-1]["logprobs"] = part["logprobs"][sent : sent + 1]
                events.append(
                    {"type": f"response.{text_event}.done", **address, text_key: part[text_key]}
                )
                events.append(
                    {"type": f"response.{part_event}.done", **address, "part": deepcopy(part)}
                )
        events.append(
            {"type": "response.output_item.done", "output_index": index, "item": deepcopy(item)}
        )
    terminal = (
        final.get("status") if final.get("status") in ("incomplete", "failed") else "completed"
    )
    events.append({"type": f"response.{terminal}", "response": final})
    return [{**event, "sequence_number": sequence} for sequence, event in enumerate(events)]


def message_events(model: str = MODEL, stop_reason: str = "end_turn") -> list[dict]:
    """A minimal Anthropic text stream, using the published Messages event lifecycle."""
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "ping"},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "o"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "k"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": 2},
        },
        {"type": "message_stop"},
    ]


def named_sse(events: list[dict]) -> str:
    return "".join(
        f"event: {body['type']}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"
        for body in events
    )


@dataclass(frozen=True)
class FakeResponsesConfig:
    """Defects and optional endpoints added with the Responses coverage slice.

    `renamed_items` changes terminal stream ids. Input defects are interpreted by
    `response_input_view` and `response_input_refusal`. Feature modes are `refuse`,
    `server_error`, `ignore`, `echo`, `wrong_echo`, `drops_sampling` and `phantom_call`. Shape
    defects are interpreted by `response_shape`. The side-endpoint defects are
    `constant`/`server_error` for input tokens and `no_item`/`server_error` for compaction.
    """

    stream_defect: str | None = None
    input_defect: str | None = None
    feature_mode: str | None = None
    shape_defect: str | None = None
    serve_input_tokens: bool = False
    input_tokens_defect: str | None = None
    serve_compact: bool = False
    compact_defect: str | None = None
    encrypted_reasoning: bool = False


@dataclass(frozen=True)
class FakeModelDiscoveryConfig:
    """Model-listing defects and support for retrieving one listed model.

    Listing defects are `missing_fields`, `retrieve_any`, `retrieve_server_error`, `redirect`,
    `detail_body` and `empty_body`; the last two return a detail-only 404 and an empty 404.
    `forbidden` and `absent` answer the list itself with a 403 and a 404.
    `model_info` is the gateway-style `GET /v1/model/info` payload for the model. None leaves
    the route unserved, as on a model server.
    """

    serve_retrieval: bool = False
    listing_defect: str | None = None
    model_info: dict | None = None
    model_info_name: str | None = None


DEFAULT_FAKE_RESPONSES_CONFIG = FakeResponsesConfig()
DEFAULT_FAKE_MODEL_DISCOVERY_CONFIG = FakeModelDiscoveryConfig()


class FakeFleet:
    """A threaded HTTP server standing in for one ingress.

    Flags let a test ask for the specific misbehaviours the suite is supposed to catch:
    `fall_back_on_unknown_model` serves a generation for a model that does not exist, and
    `abort_finish_reason` returns finish_reason "abort" on an HTTP 200.
    """

    def __init__(
        self,
        model: str = MODEL,
        inline_thinking: bool = True,
        fall_back_on_unknown_model: bool = False,
        abort_finish_reason: bool = False,
        echo_request_id: bool = False,
        serve_responses: bool = True,
        serve_chat: bool = True,
        omit_done: bool = False,
        usage_without_flag: bool = False,
        usage_before_finish: bool = False,
        two_finish_reasons: bool = False,
        throttle: bool = False,
        throttle_signals: bool = True,
        ignore_bad_max_tokens: bool = False,
        ignore_bad_effort: bool = False,
        bad_effort_status: int = 400,
        truncate_overflow: bool = False,
        stream_before_validation: bool = False,
        plain_text_errors: bool = False,
        delay_s: float = 0.0,
        tls_cert: tuple[Path, Path] | None = None,
        max_model_len: int = MAX_MODEL_LEN,
        advertised_max_model_len: int | None = None,
        overflow_error: tuple[int, str] | None = None,
        completion_overrides: dict | None = None,
        non_json_success: bool = False,
        message_overrides: dict | None = None,
        message_tools: bool = False,
        message_tool_defect: str | None = None,
        response_overrides: dict | None = None,
        response_options: bool = False,
        response_option_defect: str | None = None,
        response_tools: bool = False,
        response_tool_defect: str | None = None,
        auth_header: tuple[str, str] | None = None,
        messages_stream: str | None = None,
        messages_stream_content_type: str = "text/event-stream",
        serve_count_tokens: bool = True,
        count_tokens_response: dict | None = None,
        decreasing_token_count: bool = False,
        ignore_bad_count_tokens: bool = False,
        responses_stream: str | None = None,
        responses_stream_content_type: str = "text/event-stream",
        chat_stream_content_type: str = "text/event-stream",
        chat_overrides: dict | None = None,
        chat_options: bool = False,
        chat_option_defect: str | None = None,
        chat_tools: bool = False,
        chat_tool_defect: str | None = None,
        chat_tool_finish: str = "tool_calls",
        structured_output_defect: str | None = None,
        answer_leak: str | None = None,
        stateful_responses: bool = False,
        response_state_defect: str | None = None,
        openai_contract: bool = False,
        openai_contract_defect: str | None = None,
        openai_field_status: dict | None = None,
        accept_missing_auth: bool = False,
        ignore_api_version: bool = False,
        extra_response_headers: dict | None = None,
        vllm_options: bool = False,
        vllm_option_defect: str | None = None,
        health_status: int = 200,
        empty_chat_stream: bool = False,
        render: bool = False,
        render_defect: str | None = None,
        cache_probe: bool = False,
        cache_defect: str | None = None,
        cache_block: int = 16,
        forced_thinking_tokens: int = 0,
        cache_withheld: int = 0,
        tool_preamble: int = 0,
        header_probe_disposition: str | None = None,
        header_probe_defect: str | None = None,
        prompt_accounting: bool = False,
        context_defect: str | None = None,
        client_field_defect: str | None = None,
        effort_tokens: dict | None = None,
        messages_stream_gap_s: float = 0.0,
        long_decode_gap_s: float = 0.0,
        refuse_output_overflow: bool = False,
        blank_turn_error: bool = False,
        refuse_query_string: bool = False,
        refuse_thinking_tools: bool = False,
        refuse_null_fields: bool = False,
        burst_wedge: bool = False,
        refuse_max_completion_tokens: bool = False,
        reasoning_off_defect: str | None = None,
        effort_tool_defect: str | None = None,
        served_aliases: tuple[str, ...] = (),
        model_errors: dict[str, int] | None = None,
        quiet_efforts: tuple[str, ...] = (),
        quiet_effort_defect: str | None = None,
        effort_render: dict | None = None,
        effort_render_defect: str | None = None,
        shared_cache: dict | None = None,
        transport: dict | None = None,
        forward_defect: str | None = None,
        byte_probe: bool = False,
        byte_defect: str | None = None,
        contract_defect: str | None = None,
        anthropic: dict | None = None,
        response_config: FakeResponsesConfig = DEFAULT_FAKE_RESPONSES_CONFIG,
        model_discovery_config: FakeModelDiscoveryConfig = DEFAULT_FAKE_MODEL_DISCOVERY_CONFIG,
        tool_engine: bool = False,
        tool_engine_defect: str | None = None,
        tool_engine_warm_cache: bool = False,
        cueless_eos: bool = False,
        completions_end_at_once: bool = False,
        serve_missing_model: bool = False,
        openai_reasons: bool = False,
    ) -> None:
        self.model = model
        # A generic tool-calling model for the tools suite; see tests/tool_engine.py.
        self.tool_engine = tool_engine
        self.tool_engine_defect = tool_engine_defect
        self.tool_engine_warm_cache = tool_engine_warm_cache
        # Report input tokens that track the request, so cases can compare two prompts by size.
        self.prompt_accounting = prompt_accounting
        self.context_defect = context_defect
        self.client_field_defect = client_field_defect
        # Output tokens per requested effort; None keeps the fixed count of an unaware fleet.
        self.effort_tokens = effort_tokens
        # Seconds between Messages stream frames; zero writes them together, as a buffer would.
        self.messages_stream_gap_s = messages_stream_gap_s
        # Seconds per frame a cache-probe generation with a budget over LONG_DECODE_BUDGET takes:
        # streamed, each frame follows the last by this gap; not streamed, the answer comes after
        # all of them, as a long decode does.
        self.long_decode_gap_s = long_decode_gap_s
        # Refuse a prompt plus output budget over the window, as vLLM does. Off by default, so
        # the fake serves any budget, as a layer that clamps it would.
        self.refuse_output_overflow = refuse_output_overflow
        # Answers 500 to a conversation holding an empty or whitespace-only turn.
        self.blank_turn_error = blank_turn_error
        # A route that matches the raw path, so an agent's query string misses it.
        self.refuse_query_string = refuse_query_string
        # A stack that refuses thinking alongside tools, though it serves each alone.
        self.refuse_thinking_tools = refuse_thinking_tools
        # A stack that refuses an explicit null in a history message, which SDKs pass back.
        self.refuse_null_fields = refuse_null_fields
        # A stack that stops serving after concurrent streams, as one leaking a slot per cancel.
        self.burst_wedge = burst_wedge
        # A stack that knows only `max_tokens`, as an older Chat schema does.
        self.refuse_max_completion_tokens = refuse_max_completion_tokens
        # A stack that keeps reasoning on for a Chat schema request that turns it off.
        self.reasoning_off_defect = reasoning_off_defect
        # A stack whose automatic Chat tool call goes wrong at one reasoning setting.
        self.effort_tool_defect = effort_tool_defect
        # Other model names the fake serves as its own model, as a gateway alias does.
        self.served_aliases = served_aliases
        # A status per model name, as a layer that forwards some names elsewhere answers.
        self.model_errors = model_errors or {}
        # Chat efforts the fake answers without its inline reasoning, top level or as a template
        # kwarg; each defect leaves one carrier reasoning.
        self.quiet_efforts = quiet_efforts
        self.quiet_effort_defect = quiet_effort_defect
        # Requested effort to the level a template renders, answered with prompt logprobs that
        # spell the rendered prompt; see `effort_render_payload` for the defects.
        self.effort_render = effort_render
        self.effort_render_defect = effort_render_defect
        self.wedged = False
        # HTTP and SSE behavior and defects; see tests/transport_fakes.py.
        self.transport = transport or {}
        # How the fleet mishandles an unknown request element; see `forward_verdict`.
        self.forward_defect = forward_defect
        self.crashed = False
        # Report input size from the delivered bytes; see `rendered_size` for the defects.
        self.byte_probe = byte_probe
        self.byte_defect = byte_defect
        # A release that changes a response shape; see `drift_payload` and `drift_stream`.
        self.contract_defect = contract_defect
        # An Anthropic-faithful Messages backend; see tests/fake_anthropic.py.
        self.anthropic = anthropic
        self.response_config = response_config
        self.model_discovery_config = model_discovery_config
        self.stateful_responses = stateful_responses
        self.response_state_defect = response_state_defect
        # Chat and Completions answered by tests/fake_openai.py; see its defects.
        self.openai_contract = openai_contract
        self.openai_contract_defect = openai_contract_defect
        if openai_contract:
            from tests import fake_openai

            # Each fleet starts its noise cycles afresh, so a noisy run is reproducible.
            fake_openai.reset_effort_noise()
        self.openai_field_status = openai_field_status
        self.openai_reasons = openai_reasons
        # A model that ends a raw Completions prompt at once when the prompt leaves nothing to
        # continue (see `cueless`): empty text, one token, finish stop, in JSON or a stream.
        # Healthy: with no chat template and no cue, end of text is a fair continuation.
        self.cueless_eos = cueless_eos
        # The defect: every Completions reply ends at once, open sentence or not.
        self.completions_end_at_once = completions_end_at_once
        # Serve a request without a model from the deployed one, as a single-model vLLM does.
        self.serve_missing_model = serve_missing_model
        self.response_store = {}
        # The input each stored response was generated from, for previous_response_id chains.
        self.response_context = {}
        self.accept_missing_auth = accept_missing_auth
        self.ignore_api_version = ignore_api_version
        self.extra_response_headers = extra_response_headers or {}
        self.vllm_options = vllm_options
        self.vllm_option_defect = vllm_option_defect
        self.health_status = health_status
        self.empty_chat_stream = empty_chat_stream

        self.render = render
        self.render_defect = render_defect
        self.render_calls = 0

        # A prefix cache of whole blocks per cache_salt; see `prefix_cache` for its defects.
        self.cache_probe = cache_probe
        self.cache_defect = cache_defect
        self.cache_block = cache_block
        # A model that cannot turn thinking off: every cache-probe Messages reply thinks this
        # many tokens before it answers, whatever the request asked.
        self.forced_thinking_tokens = forced_thinking_tokens
        # Trailing blocks of every hit the engine recomputes rather than reads, as vLLM does for
        # a speculative-decoding draft's KV: by design on such a deployment, not a defect.
        self.cache_withheld = cache_withheld
        # Words of fixed tool instructions the template renders after the system prompt and
        # before the tool schemas, the same in every tools request. Healthy: a template with a
        # fixed tools section renders them so, and every engine caches them like any prefix.
        self.tool_preamble = tool_preamble
        self.cache_store: dict[str, set[tuple[str, ...]]] = {}
        if shared_cache is not None:
            # Another fake's store: two layers in front of the same engines.
            self.cache_store = shared_cache
        self.cache_requests = 0
        # The first user turn of every conversation served, for `session_by_head`.
        self.cache_heads: set[str] = set()
        # The unrendered fields of every request served, for `fields_keyed`.
        self.cache_placements: set[str] = set()
        # Requests inside `prefix_cache` at once, for `concurrent_cold`.
        self.cache_lock = threading.Lock()
        self.cache_inflight = 0
        self.header_probe_disposition = header_probe_disposition
        self.header_probe_defect = header_probe_defect
        # (certificate, key): serve HTTPS with them, as a cluster's private ingress CA would.
        self.tls_cert = tls_cert
        self.inline_thinking = inline_thinking
        self.fall_back_on_unknown_model = fall_back_on_unknown_model
        self.abort_finish_reason = abort_finish_reason
        self.completion_overrides = completion_overrides or {}
        # Includes reasoning-only HTTP 200 responses that ordinary smoke cases accept.
        self.chat_overrides = chat_overrides or {}
        self.chat_options = chat_options
        self.chat_option_defect = chat_option_defect

        self.chat_tools = chat_tools
        self.chat_tool_defect = chat_tool_defect
        # The finish of a forced call: tool_calls, or stop where a profile declares it.
        self.chat_tool_finish = chat_tool_finish
        self.non_json_success = non_json_success
        self.message_overrides = message_overrides or {}
        self.message_tools = message_tools
        self.message_tool_defect = message_tool_defect
        self.response_overrides = response_overrides or {}
        self.response_options = response_options
        self.response_option_defect = response_option_defect

        self.response_tools = response_tools
        self.response_tool_defect = response_tool_defect
        self.auth_header = auth_header
        self.messages_stream = messages_stream
        self.messages_stream_content_type = messages_stream_content_type
        self.serve_count_tokens = serve_count_tokens
        self.count_tokens_response = count_tokens_response
        self.decreasing_token_count = decreasing_token_count
        self.ignore_bad_count_tokens = ignore_bad_count_tokens
        self.responses_stream = responses_stream
        self.responses_stream_content_type = responses_stream_content_type
        self.chat_stream_content_type = chat_stream_content_type
        self.structured_output_defect = structured_output_defect
        # Reasoning that reaches the answer channel; see LEAKS.
        self.answer_leak = answer_leak
        self.echo_request_id = echo_request_id
        self.serve_responses = serve_responses
        self.serve_chat = serve_chat
        # Streaming misbehaviours the suite is supposed to catch. Each is a real shape a stack
        # can produce: a missing sentinel hangs a reader, usage without the flag truncates a
        # client that stops at the first choice-less frame, and usage before the finish reason
        # is never seen by a client that stops at the finish reason.
        self.omit_done = omit_done
        self.usage_without_flag = usage_without_flag
        self.usage_before_finish = usage_before_finish
        self.two_finish_reasons = two_finish_reasons
        # Unhappy-path misbehaviours. Each is a real way a stack mishandles a bad request:
        # accepting a negative token limit or invalid effort, silently truncating an over-length
        # prompt so the answer comes from a prompt the client did not send, opening a 200 stream
        # before validating and reporting the failure inside a frame, and answering with prose
        # no SDK can parse.
        # Refuses generation with 429, as a gateway enforcing a per-key quota does.
        # `throttle_signals` off reproduces a gateway that throttles but says nothing about
        # how long to wait, which is the harder case for whoever investigates.
        self.throttle = throttle
        self.throttle_signals = throttle_signals
        self.ignore_bad_max_tokens = ignore_bad_max_tokens
        self.ignore_bad_effort = ignore_bad_effort
        self.bad_effort_status = bad_effort_status
        self.truncate_overflow = truncate_overflow
        self.max_model_len = max_model_len
        # The context length GET /v1/models reports for the model; None omits the field, as a
        # gateway may.
        self.advertised_max_model_len = advertised_max_model_len
        # Substitute an intermediary rejection for the engine's context-limit error.
        self.overflow_error = overflow_error
        self.stream_before_validation = stream_before_validation
        self.plain_text_errors = plain_text_errors
        # Seconds every POST waits before answering, so a test can interrupt a run mid-flight.
        self.delay_s = delay_s
        self.seen: list[tuple[str, str, dict]] = []

        fleet = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: A002 - silence the default access log
                pass

            def _send(self, code: int, payload: dict, extra_headers: dict | None = None) -> None:
                payload = drift_payload(fleet.contract_defect, code, payload)
                # Keep the generated JSON intact while corrupting its completion envelope.
                if code == 200 and self.path.rstrip("/") in (
                    "/v1/chat/completions",
                    "/v1/messages",
                    "/v1/responses",
                ):
                    defect = fleet.structured_output_defect
                    if defect == "wrong_model_echo":
                        payload["model"] = "another/model"
                    elif defect == "wrong_object":
                        field = "type" if self.path.rstrip("/") == "/v1/messages" else "object"
                        payload[field] = "wrong_object"
                    elif defect == "wrong_role":
                        if "choices" in payload:
                            payload["choices"][0]["message"]["role"] = "user"
                        elif payload.get("type") == "message":
                            payload["role"] = "user"
                if code == 200 and self._ends_at_once():
                    end_at_once(payload)
                if code == 200 and fleet.byte_probe and getattr(self, "llmdv_size", None):
                    account_input(payload, self.llmdv_size)
                if code == 200 and fleet.byte_defect == "sorted_keys":
                    sort_answer_keys(payload)
                if (
                    self.path.rstrip("/") in ("/v1/messages", COUNT_TOKENS_PATH)
                    and "error" in payload
                ):
                    payload = {"type": "error", "error": payload["error"]}
                if fleet.anthropic is not None and self.path.rstrip("/") in (
                    "/v1/messages",
                    COUNT_TOKENS_PATH,
                ):
                    payload = anthropic_error(fleet.anthropic, code, payload)
                payload = transport_fakes.respond(fleet, self, code, payload)
                unstructured = fleet.plain_text_errors and code >= 400
                non_json = (
                    fleet.non_json_success
                    and code == 200
                    and (
                        "choices" in payload
                        or payload.get("type") == "message"
                        or payload.get("object") == "response"
                        or self.path.rstrip("/") == COUNT_TOKENS_PATH
                    )
                )
                if non_json:
                    body = b"upstream returned an invalid response"
                elif unstructured:
                    message = (payload.get("error") or {}).get("message", "error")
                    body = f"inference error: {code} - {message}".encode()
                else:
                    body = json.dumps(payload).encode()
                body, coding = transport_fakes.response_headers(fleet, self, code, body)
                self.send_response(code)
                self.send_header(
                    "Content-Type", "text/plain" if unstructured or non_json else "application/json"
                )
                self.send_header("Content-Length", str(len(body)))
                for name, value in coding.items():
                    self.send_header(name, value)
                if fleet.echo_request_id and self.headers.get("x-request-id"):
                    self.send_header("x-request-id", self.headers["x-request-id"])
                for name, value in {
                    **fleet.extra_response_headers,
                    **(extra_headers or {}),
                }.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _rejects_key(self) -> bool:
                header, expected = fleet.auth_header
                presented = self.headers.get(header)
                return presented != expected and not (fleet.accept_missing_auth and not presented)

            def do_GET(self):
                if transport_fakes.get(fleet, self):
                    return None
                path = self.path.rstrip("/")
                if path in ("/health", ""):
                    return self._send(fleet.health_status, {"status": "ok"})
                # A gateway authenticates its model list as it does generation (LiteLLM
                # answers a bad key with 401 here too); only the health path is exempt.
                if (
                    fleet.auth_header is not None
                    and path.startswith(("/v1/models", "/v1/model/info"))
                    and self._rejects_key()
                ):
                    return self._send(
                        401, {"error": {"type": "authentication_error", "message": "bad key"}}
                    )
                if path == "/v1/models" and fleet.model_discovery_config.listing_defect in (
                    "forbidden",
                    "absent",
                ):
                    if fleet.model_discovery_config.listing_defect == "forbidden":
                        return self._send(
                            403, {"error": {"type": "permission_error", "message": "forbidden"}}
                        )
                    return self._send(404, {"detail": "Not Found"})
                if (
                    path.startswith("/v1/responses/")
                    and fleet.response_state_defect == "retrieve_server_error"
                ):
                    return self._send(500, {"error": {"message": "store unavailable"}})
                if fleet.stateful_responses and path.startswith("/v1/responses/"):
                    stored = fleet.response_store.get(path.rsplit("/", 1)[-1])
                    if stored is None or fleet.response_state_defect == "missing":
                        return self._send(
                            404, {"error": {"message": "not stored", "type": "not_found"}}
                        )
                    result = deepcopy(stored)
                    if fleet.response_state_defect == "changed_id":
                        result["id"] = "resp_other"
                    if fleet.response_state_defect == "changed_output" and result["output"]:
                        result["output"][0]["content"][0]["text"] = "other"
                    return self._send(200, result)
                if path == "/v1/model/info" and fleet.model_discovery_config.model_info is not None:
                    config = fleet.model_discovery_config
                    return self._send(
                        200,
                        {
                            "data": [
                                {
                                    "model_name": config.model_info_name or fleet.model,
                                    "litellm_params": {"model": f"openai/{fleet.model}"},
                                    "model_info": dict(config.model_info),
                                }
                            ]
                        },
                    )
                if path.startswith("/v1/models/"):
                    requested = unquote(path.removeprefix("/v1/models/"))
                    entry = {"id": fleet.model, "object": "model", "owned_by": "fake", "created": 0}
                    defect = fleet.model_discovery_config.listing_defect
                    if defect == "detail_body":
                        return self._send(404, {"detail": "Not Found"})
                    if defect == "empty_body":
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return None
                    if defect == "retrieve_server_error":
                        return self._send(500, {"error": {"message": "internal error"}})
                    if defect == "redirect":
                        return self._send(307, {}, extra_headers={"Location": "/v1/models"})
                    if defect == "retrieve_any" or (
                        fleet.model_discovery_config.serve_retrieval and requested == fleet.model
                    ):
                        return self._send(200, entry)
                    return self._send(
                        404, {"error": {"message": "model not found", "type": "NotFoundError"}}
                    )
                if (
                    path == "/v1/models"
                    and fleet.model_discovery_config.listing_defect == "missing_fields"
                ):
                    return self._send(
                        200, {"object": "list", "data": [{"id": fleet.model, "object": "model"}]}
                    )
                if path == "/v1/models":
                    return self._send(
                        200,
                        {
                            "object": "list",
                            "data": [
                                {
                                    "id": fleet.model,
                                    "object": "model",
                                    "owned_by": "fake",
                                    "created": 0,
                                    **(
                                        {}
                                        if fleet.advertised_max_model_len is None
                                        else {"max_model_len": fleet.advertised_max_model_len}
                                    ),
                                }
                            ],
                        },
                    )
                if path in POST_PATHS:
                    return self._send(
                        405,
                        {
                            "error": {
                                "message": "Method Not Allowed",
                                "type": "invalid_request_error",
                            }
                        },
                    )
                return self._send(404, {"error": {"message": "Not Found", "type": "NotFoundError"}})

            def do_HEAD(self):
                transport_fakes.head(fleet, self)

            def do_OPTIONS(self):
                transport_fakes.options(fleet, self)

            def handle_expect_100(self):
                return transport_fakes.expect_100(fleet, self)

            def _invalid(self, body, path="/v1/chat/completions"):
                """The first validation error in `body`, as an (code, payload) pair, or None.

                Ordered the way a real server validates: shape before values, values before
                tools. Unknown top-level fields are deliberately NOT rejected -- an
                OpenAI-compatible server ignores them, and the profile declares that.

                Completions takes a prompt string; Messages requires max_tokens and validates
                output_config.effort; Responses uses input and max_output_tokens. Each has its
                own tool grammar.
                """

                def bad(message, param=None):
                    err = {"message": message, "type": "invalid_request_error"}
                    if param:
                        err["param"] = param
                    return 400, {"error": err}

                if path == "/v1/completions":
                    prompt = body.get("prompt")
                    if prompt is None:
                        return bad("prompt is required", "prompt")
                    if not isinstance(prompt, str):
                        return bad("prompt must be a string", "prompt")
                    field = "prompt"
                elif path == "/v1/responses":
                    prompt = body.get("input")
                    if prompt is None:
                        return bad("input is required", "input")
                    if not isinstance(prompt, (str, list)):
                        return bad("input must be a string or an array", "input")
                    if isinstance(prompt, list):
                        prompt = " ".join(
                            str(item.get("content", ""))
                            for item in prompt
                            if isinstance(item, dict)
                        )
                    field = "input"
                else:
                    messages = body.get("messages")
                    if messages is None:
                        return bad("messages is required", "messages")
                    if not isinstance(messages, list):
                        return bad("messages must be a list", "messages")
                    prompt = " ".join(
                        str(m.get("content", "")) for m in messages if isinstance(m, dict)
                    )
                    field = "messages"

                token_field = "max_output_tokens" if path == "/v1/responses" else "max_tokens"
                max_tokens = body.get(token_field)
                if path == "/v1/messages" and not isinstance(max_tokens, int):
                    return bad("max_tokens is required and must be an integer", "max_tokens")
                if (
                    isinstance(max_tokens, int)
                    and max_tokens < 0
                    and not fleet.ignore_bad_max_tokens
                ):
                    return bad(f"{token_field} must be non-negative", token_field)

                output_config = body.get("output_config")
                if (
                    path == "/v1/messages"
                    and isinstance(output_config, dict)
                    and "effort" in output_config
                    and output_config["effort"] not in MESSAGES_EFFORTS
                    and not fleet.ignore_bad_effort
                ):
                    _, payload = bad(
                        f"output_config.effort must be one of {list(MESSAGES_EFFORTS)}",
                        "output_config.effort",
                    )
                    return fleet.bad_effort_status, payload

                n = body.get("n", 1)
                if (
                    path == "/v1/chat/completions"
                    and body.get("temperature") == 0
                    and isinstance(n, int)
                    and n > 1
                ):
                    return bad(f"n must be 1 when using greedy sampling, got {n}.", "n")

                if len(prompt.split()) > fleet.max_model_len and not fleet.truncate_overflow:
                    if fleet.overflow_error is not None:
                        status, message = fleet.overflow_error
                        _, payload = bad(message, field)
                        return status, payload
                    return bad(
                        f"this model's maximum context length is {fleet.max_model_len} tokens",
                        field,
                    )
                if (
                    fleet.refuse_output_overflow
                    and isinstance(max_tokens, int)
                    and len(prompt.split()) + max_tokens > fleet.max_model_len
                ):
                    return bad(
                        f"This model's maximum context length is {fleet.max_model_len} tokens. "
                        f"However, you requested {max_tokens} output tokens and your prompt "
                        f"contains at least {len(prompt.split())} input tokens.",
                        token_field,
                    )
                if path == "/v1/completions":
                    return None

                if path == "/v1/responses":
                    tools = body.get("tools") or []
                    for tool in tools:
                        if (
                            not isinstance(tool, dict)
                            or tool.get("type") != "function"
                            or not isinstance(tool.get("name"), str)
                            or not tool["name"]
                        ):
                            return bad("function tool requires a flat name", "tools")
                    choice = body.get("tool_choice")
                    if choice is not None and choice not in ("auto", "none", "required"):
                        if (
                            not isinstance(choice, dict)
                            or choice.get("type") != "function"
                            or choice.get("name") not in {t["name"] for t in tools}
                        ):
                            return bad("invalid or undeclared tool choice", "tool_choice")
                    return None

                if path in ("/v1/messages", COUNT_TOKENS_PATH):
                    tools = body.get("tools") or []
                    for tool in tools:
                        if (
                            not isinstance(tool, dict)
                            or not tool.get("name")
                            or not isinstance(tool.get("input_schema"), dict)
                        ):
                            return bad("tool requires name and input_schema", "tools")
                    choice = body.get("tool_choice")
                    if choice is not None and (
                        not isinstance(choice, dict)
                        or choice.get("type") not in {"auto", "any", "tool", "none"}
                        or (
                            choice.get("type") == "tool"
                            and choice.get("name") not in {t["name"] for t in tools}
                        )
                    ):
                        return bad("invalid or undeclared tool choice", "tool_choice")
                    return None

                for tool in body.get("tools") or []:
                    if not isinstance(tool, dict) or not tool.get("function"):
                        return bad("tool entry requires a function definition", "tools")

                choice = body.get("tool_choice")
                if isinstance(choice, str) and choice not in ("auto", "none", "required"):
                    return bad(f"tool_choice {choice!r} names no declared tool", "tool_choice")

                return None

            def _ends_at_once(self) -> bool:
                """Whether this Completions request gets a reply that ends at once."""
                prompt = (getattr(self, "llmdv_body", None) or {}).get("prompt")
                return self.path.rstrip("/") == "/v1/completions" and (
                    fleet.completions_end_at_once or (fleet.cueless_eos and cueless(prompt))
                )

            def _named_stream(self, stream, content_type, extra_headers=None, gap_s=0.0):
                if self._ends_at_once():
                    stream = end_stream_at_once(stream)
                stream = drift_stream(fleet.contract_defect, stream)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                for name, value in (extra_headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if fleet.messages_stream_gap_s and "message_start" in stream:
                    gap_s = fleet.messages_stream_gap_s
                if gap_s:
                    for frame in stream.split("\n\n"):
                        if frame:
                            self.wfile.write((frame + "\n\n").encode("utf-8"))
                            self.wfile.flush()
                            time.sleep(gap_s)
                    return
                self.wfile.write(stream.encode("utf-8"))
                self.wfile.flush()

            def _stream(self, body):
                """Emit an OpenAI-shaped SSE stream, with whichever defect is configured."""
                if transport_fakes.stream(fleet, self, body, drift=drift_stream):
                    return None
                include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
                answer = structured_answer(
                    body, self.path.rstrip("/"), fleet.structured_output_defect
                )
                if answer is None:
                    spoken = leaked_answer(spoken_answer(body), fleet.answer_leak)
                    # A leak arrives over two frames, so only the assembled answer shows it.
                    half = len(spoken) // 2 if fleet.answer_leak else len(spoken)
                    answer = [
                        "<think>",
                        "weighing",
                        " it up",
                        "</think>",
                        spoken[:half],
                        spoken[half:],
                    ]
                deltas = [piece for piece in answer if piece]
                usage = {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}

                def frame(obj):
                    return drift_stream(
                        fleet.contract_defect, f"data: {json.dumps(obj)}\n\n"
                    ).encode()

                def chunk(delta, finish=None, choices=True, with_usage=False):
                    out = {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "model": fleet.model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
                        if choices
                        else [],
                    }
                    if with_usage:
                        out["usage"] = usage
                    return out

                # No Content-Length is possible for a stream, and this handler speaks
                # HTTP/1.1, so the body has to be delimited some other way or the client waits
                # forever for an end that never comes. Closing the connection is what a real
                # SSE endpoint does, and httpx reads to EOF.
                self.send_response(200)
                self.send_header("Content-Type", fleet.chat_stream_content_type)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

                if fleet.stream_before_validation and self._invalid(body, self.path.rstrip("/")):
                    # The defect: a 200 stream was already promised, so the client has to parse
                    # an error out of a transport it was told had succeeded.
                    self.wfile.write(
                        frame(
                            {
                                "error": {
                                    "message": "invalid request",
                                    "type": "invalid_request_error",
                                }
                            }
                        )
                    )
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return

                self.wfile.write(frame(chunk({"role": "assistant", "content": ""})))
                if fleet.usage_before_finish or (fleet.usage_without_flag and not include_usage):
                    self.wfile.write(frame(chunk({}, choices=False, with_usage=True)))
                for piece in [] if fleet.empty_chat_stream else deltas:
                    self.wfile.write(frame(chunk({"content": piece})))
                finish = "abort" if fleet.abort_finish_reason else "stop"
                self.wfile.write(frame(chunk({}, finish=finish)))
                if fleet.two_finish_reasons:
                    self.wfile.write(frame(chunk({}, finish="stop")))
                if include_usage and not fleet.usage_before_finish:
                    self.wfile.write(frame(chunk({}, choices=False, with_usage=True)))
                if not fleet.omit_done:
                    self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def do_POST(self):
                if fleet.delay_s:
                    time.sleep(fleet.delay_s)
                # transport_fakes.post reads the body, chunked or not, and returns it.
                answered, raw = transport_fakes.post(fleet, self)
                if answered:
                    return None
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    return self._send(
                        400, {"error": {"message": "invalid JSON", "type": "invalid_request_error"}}
                    )

                # A query string, such as an agent's `?beta=true`, names the same route.
                self.path, _, query = self.path.partition("?")
                path = self.path.rstrip("/")
                fleet.seen.append((self.command, path, dict(self.headers)))
                self.llmdv_body = body
                self.llmdv_size = (
                    rendered_size(raw, fleet.byte_defect) if fleet.byte_probe else None
                )
                if fleet.forward_defect or fleet.crashed:
                    verdict = forward_verdict(fleet, raw, self.headers, path)
                    if verdict == "drop":
                        self.close_connection = True
                        return
                    if verdict == "strip_tools":
                        body.pop("tools", None)
                    elif verdict is not None:
                        return self._send(*verdict)

                if path == "/v1/responses/compact" and (
                    fleet.response_config.serve_compact or fleet.response_config.compact_defect
                ):
                    if fleet.response_config.compact_defect == "server_error":
                        return self._send(500, {"error": {"message": "internal error"}})
                    users = [
                        i
                        for i in body.get("input") or []
                        if isinstance(i, dict) and i.get("role") == "user"
                    ]
                    output = [
                        {"type": "message", "id": f"msg_c{n}", **u} for n, u in enumerate(users)
                    ]
                    if fleet.response_config.compact_defect != "no_item":
                        output.append(
                            {"type": "compaction", "id": "cmp_fake", "encrypted_content": "x"}
                        )
                    usage = {"input_tokens": 9, "output_tokens": 3, "total_tokens": 12}
                    return self._send(
                        200,
                        {
                            "id": "resp_compact",
                            "object": "response.compaction",
                            "created_at": 0,
                            "output": output,
                            "usage": usage,
                        },
                    )
                if path == "/v1/responses/input_tokens" and (
                    fleet.response_config.serve_input_tokens
                    or fleet.response_config.input_tokens_defect
                ):
                    if fleet.response_config.input_tokens_defect == "server_error":
                        return self._send(500, {"error": {"message": "internal error"}})
                    counted = json.dumps([body.get("instructions"), body.get("input")])
                    count = (
                        7
                        if fleet.response_config.input_tokens_defect == "constant"
                        else len(counted.split())
                    )
                    return self._send(
                        200, {"object": "response.input_tokens", "input_tokens": count}
                    )

                if query and fleet.refuse_query_string:
                    return self._send(404, {"error": {"message": "Not Found", "type": "not_found"}})
                if fleet.refuse_thinking_tools and "thinking" in body and body.get("tools"):
                    error = {"message": "thinking is not supported with tools"}
                    return self._send(400, {"error": {**error, "type": "invalid_request_error"}})
                if fleet.wedged and path in POST_PATHS:
                    return self._send(503, {"error": {"message": "no free slot", "type": "busy"}})
                if fleet.burst_wedge and body.get("stream") and fleet.overlaps_another():
                    fleet.wedged = True
                if fleet.refuse_max_completion_tokens and "max_completion_tokens" in body:
                    error = {"message": "unknown field max_completion_tokens"}
                    return self._send(400, {"error": {**error, "type": "invalid_request_error"}})
                if fleet.refuse_null_fields and any(
                    isinstance(m, dict) and None in m.values() for m in body.get("messages") or []
                ):
                    error = {"message": "null field in a message", "type": "invalid_request_error"}
                    return self._send(400, {"error": error})
                if fleet.blank_turn_error and has_blank_turn(body):
                    return self._send(
                        500, {"error": {"message": "Internal Server Error", "type": "internal"}}
                    )
                if (
                    fleet.reasoning_off_defect
                    and path == "/v1/chat/completions"
                    and "reasoning_effort" in body
                    and body.get("response_format")
                ):
                    payload = reasoning_off_payload(body, fleet.model, fleet.reasoning_off_defect)
                    return self._send(200, payload)
                payload = (
                    effort_tool_payload(body, fleet.model, fleet.effort_tool_defect)
                    if fleet.effort_tool_defect and path == "/v1/chat/completions"
                    else None
                )
                if payload is not None:
                    if body.get("stream"):
                        return self._named_stream(chat_text_stream(payload), "text/event-stream")
                    return self._send(200, payload)
                if fleet.effort_render is not None and "prompt_logprobs" in body:
                    return self._send(
                        200,
                        effort_render_payload(
                            path, body, fleet.model, fleet.effort_render, fleet.effort_render_defect
                        ),
                    )
                if fleet.quiet_efforts and path == "/v1/chat/completions":
                    payload = quiet_effort_payload(
                        body, fleet.model, fleet.quiet_efforts, fleet.quiet_effort_defect
                    )
                    if payload is not None:
                        return self._send(200, payload)

                if (
                    fleet.stateful_responses
                    and path.startswith("/v1/responses/")
                    and path.endswith("/cancel")
                ):
                    stored = fleet.response_store.get(path.split("/")[-2])
                    if stored is None:
                        return self._send(404, {"error": {"message": "not stored"}})
                    if fleet.response_state_defect == "race":
                        stored.update(response_body(fleet.model))
                        return self._send(400, {"error": {"message": "already completed"}})
                    result = {**stored, "status": "cancelled"}
                    if fleet.response_state_defect != "not_persisted":
                        fleet.response_store[stored["id"]] = result
                    if fleet.response_state_defect == "ignored_cancel":
                        result = stored
                    return self._send(200, result)
                previous = body.get("previous_response_id") if path == "/v1/responses" else None
                context = None
                if previous is not None and fleet.response_state_defect != "ignores_previous_id":
                    if fleet.response_store.get(previous) is None:
                        return self._send(
                            404,
                            {
                                "error": {
                                    "message": f"previous response {previous} not found",
                                    "type": "invalid_request_error",
                                    "param": "previous_response_id",
                                }
                            },
                        )
                    if fleet.response_state_defect != "chain_ignored":
                        context = fleet.response_context.get(previous)
                if (
                    path == "/v1/responses"
                    and body.get("background")
                    and not fleet.stateful_responses
                    and fleet.response_state_defect != "accepts_background"
                ):
                    return self._send(
                        400,
                        {
                            "error": {
                                "message": "background requires a response store",
                                "type": "invalid_request_error",
                                "param": "background",
                            }
                        },
                    )
                if fleet.stateful_responses and path == "/v1/responses" and body.get("store"):
                    result = response_body(fleet.model)
                    word = code_word_answer(context, body.get("input"))
                    if word is not None:
                        result["output"][0]["content"][0]["text"] = word
                    fleet.response_context[result["id"]] = [context, body.get("input")]
                    if body.get("background") and fleet.response_state_defect != "fast":
                        result.update(status="queued", output=[], usage=None)
                    fleet.response_store[result["id"]] = deepcopy(result)
                    return self._send(200, result)

                if fleet.render and path in ABSENT_PATHS:
                    if body.get("model") != fleet.model:
                        return self._send(
                            404, {"error": {"type": "not_found", "message": "unknown model"}}
                        )
                    fleet.render_calls += 1
                    return self._send(
                        200,
                        render_payload(
                            body, path, fleet.model, fleet.render_defect, fleet.render_calls
                        ),
                    )
                if fleet.throttle and path in POST_PATHS:
                    extra = (
                        {"Retry-After": "7", "x-ratelimit-remaining-requests": "0"}
                        if fleet.throttle_signals
                        else {}
                    )
                    return self._send(
                        429,
                        {"error": {"message": "rate limit exceeded", "type": "rate_limit_error"}},
                        extra_headers=extra,
                    )

                if fleet.auth_header is not None and path in POST_PATHS and self._rejects_key():
                    return self._send(
                        401, {"error": {"type": "authentication_error", "message": "bad key"}}
                    )
                if (
                    path in ("/v1/messages", COUNT_TOKENS_PATH)
                    and self.headers.get("anthropic-version") != "2023-06-01"
                    and not fleet.ignore_api_version
                ):
                    return self._send(
                        400,
                        {
                            "error": {
                                "type": "invalid_request_error",
                                "message": "missing API version",
                            }
                        },
                    )

                if path in ABSENT_PATHS:
                    return self._send(404, {"detail": "Not Found"})
                if path == "/v1/responses" and not fleet.serve_responses:
                    return self._send(404, {"detail": "Not Found"})
                if path == "/v1/chat/completions" and not fleet.serve_chat:
                    return self._send(404, {"detail": "Not Found"})
                if path == COUNT_TOKENS_PATH and not fleet.serve_count_tokens:
                    return self._send(404, {"detail": "Not Found"})
                if path not in POST_PATHS:
                    return self._send(
                        404, {"error": {"message": "Not Found", "type": "NotFoundError"}}
                    )

                variant = transport_fakes.validate(fleet, self, path, body)
                if variant is not None:
                    return self._send(*variant)
                replay = transport_fakes.idempotent(fleet, self, path)
                if replay is not None:
                    return self._send(200, replay)
                requested = body.get("model") or (
                    fleet.model if fleet.serve_missing_model else None
                )
                if (
                    fleet.openai_contract
                    and requested == fleet.model
                    and path in ("/v1/chat/completions", "/v1/completions")
                ):
                    from tests import fake_openai

                    status, payload, stream = fake_openai.reply(
                        path,
                        body,
                        fleet.model,
                        fleet.openai_contract_defect,
                        fleet.openai_field_status,
                        fleet.openai_reasons,
                    )
                    if stream is not None:
                        return self._named_stream(stream, "text/event-stream")
                    return self._send(status, payload)

                if not requested:
                    return self._send(
                        400,
                        {
                            "error": {
                                "message": "model is required",
                                "type": "invalid_request_error",
                                "param": "model",
                            }
                        },
                    )
                if requested in fleet.model_errors:
                    error = {"message": "upstream unavailable", "type": "upstream_error"}
                    return self._send(fleet.model_errors[requested], {"error": error})
                if requested in fleet.served_aliases:
                    requested = fleet.model
                if requested != fleet.model and not fleet.fall_back_on_unknown_model:
                    return self._send(
                        404,
                        {
                            "error": {
                                "message": f"The model `{requested}` does not exist.",
                                "type": "NotFoundError",
                                "param": "model",
                                "code": 404,
                            }
                        },
                    )

                mode = fleet.response_config.feature_mode
                if path == "/v1/responses" and mode and requested_features(body):
                    if mode == "server_error":
                        return self._send(500, {"error": {"message": "internal error"}})
                    if mode == "refuse":
                        return self._send(
                            400,
                            {"error": {"message": "unsupported", "type": "invalid_request_error"}},
                        )
                    payload = feature_payload(body, fleet.model, mode)
                    payload.update(fleet.response_overrides)
                    if not body.get("stream"):
                        return self._send(200, payload)
                    events = response_events(response=payload)
                    for event in events:
                        if mode == "wrong_echo" and event["type"].endswith(".delta"):
                            event["obfuscation"] = "padding"
                    return self._named_stream(named_sse(events), "text/event-stream")

                if fleet.tool_engine:
                    from tests import tool_engine

                    if tool_engine.handles(path, body):
                        status, payload, stream = tool_engine.serve(
                            path,
                            body,
                            fleet.model,
                            fleet.tool_engine_defect,
                            fleet.tool_engine_warm_cache,
                        )
                        if stream is not None:
                            return self._named_stream(stream, "text/event-stream")
                        return self._send(status, payload)

                invalid = self._invalid(body, path)
                if path == COUNT_TOKENS_PATH and fleet.ignore_bad_count_tokens:
                    invalid = None
                if invalid and not (body.get("stream") and fleet.stream_before_validation):
                    return self._send(*invalid)
                if invalid and body.get("stream") and fleet.stream_before_validation:
                    return self._stream(body)

                if path == "/v1/messages" and (
                    (
                        fleet.client_field_defect == "rejects_beta"
                        and self.headers.get("anthropic-beta")
                    )
                    or (
                        fleet.client_field_defect == "rejects_context_management"
                        and "context_management" in body
                    )
                ):
                    return self._send(
                        400,
                        {
                            "type": "error",
                            "error": {
                                "type": "invalid_request_error",
                                "message": "Extra inputs are not permitted",
                            },
                        },
                    )

                if fleet.anthropic is not None and path in ("/v1/messages", COUNT_TOKENS_PATH):
                    status, reply = anthropic_reply(fleet.anthropic, path, body, fleet.model)
                    if isinstance(reply, str):
                        return self._named_stream(reply, "text/event-stream")
                    return self._send(status, reply)

                if path == COUNT_TOKENS_PATH:
                    # A synthetic count, not a tokenizer: enough to test the wire contract
                    # and growth when a conversation is extended.
                    count = len(json.dumps(body).split())
                    if fleet.prompt_accounting:
                        count = prompt_words(body)
                        if fleet.context_defect == "count_ignores_system":
                            count = prompt_words({"messages": body["messages"][-1:]})
                    if fleet.decreasing_token_count:
                        count = 100 // len(body["messages"])
                    payload = {"input_tokens": count}
                    if fleet.count_tokens_response is not None:
                        payload = fleet.count_tokens_response
                    return self._send(200, payload)

                if fleet.vllm_options and path in ("/v1/chat/completions", "/v1/responses"):
                    return self._send(
                        200, vllm_option_payload(body, path, fleet.model, fleet.vllm_option_defect)
                    )

                if (
                    fleet.chat_options
                    and path == "/v1/chat/completions"
                    and ("n" in body or "logprobs" in body)
                ):
                    payload, stream = chat_choice_payload(
                        body, fleet.model, fleet.chat_option_defect
                    )
                    if body.get("stream"):
                        return self._named_stream(stream, "text/event-stream")
                    return self._send(200, payload)

                if fleet.chat_tools and path == "/v1/chat/completions" and body.get("tools"):
                    payload = chat_tool_payload(
                        body, fleet.model, fleet.chat_tool_defect, fleet.chat_tool_finish
                    )
                    if body.get("stream"):
                        return self._named_stream(
                            chat_tool_stream(payload, fleet.chat_tool_defect), "text/event-stream"
                        )
                    return self._send(200, payload)

                if fleet.message_tools and path == "/v1/messages" and body.get("tools"):
                    build = (
                        message_parallel_payload if len(body["tools"]) > 1 else message_tool_payload
                    )
                    payload = build(body, fleet.model, fleet.message_tool_defect)
                    if fleet.prompt_accounting:
                        payload["usage"]["input_tokens"] = prompt_words(body, fleet.context_defect)
                    if body.get("stream"):
                        return self._named_stream(
                            named_sse(message_tool_events(payload, fleet.message_tool_defect)),
                            "text/event-stream",
                        )
                    return self._send(200, payload)

                if fleet.response_tools and path == "/v1/responses" and body.get("tools"):
                    payload = response_tool_payload(body, fleet.model, fleet.response_tool_defect)
                    if body.get("stream"):
                        events = response_tool_events(payload, fleet.response_tool_defect)
                        if fleet.response_config.stream_defect == "renamed_items":
                            for entry in events[-1]["response"]["output"]:
                                entry["id"] = f"renamed_{entry['id']}"
                        return self._named_stream(named_sse(events), "text/event-stream")
                    return self._send(200, payload)

                if fleet.cache_probe and path in (
                    "/v1/chat/completions",
                    "/v1/messages",
                    "/v1/responses",
                ):
                    kwarg = (body.get("chat_template_kwargs") or {}).get("reasoning_effort")
                    if (
                        fleet.cache_defect == "kwargs_skip_effort_map"
                        and kwarg is not None
                        and kwarg not in TEMPLATE_EFFORTS
                    ):
                        # A deployment's effort map rewrites the top-level field only; a kwarg
                        # reaches the template as sent, which refuses a value it does not know.
                        message = f"Unsupported reasoning_effort: {kwarg}."
                        error = {"message": message, "type": "BadRequestError", "code": 400}
                        return self._send(400, {"error": error})
                    prompt, cached, replica = fleet.prefix_cache(body)
                    counts = (path, fleet.model, body, prompt, cached, fleet.cache_defect)
                    budget = body.get("max_output_tokens", body.get("max_tokens")) or 0
                    gap = fleet.long_decode_gap_s if budget > LONG_DECODE_BUDGET else 0.0
                    if body.get("stream"):
                        return self._named_stream(
                            cache_stream(*counts),
                            "text/event-stream",
                            {"x-llmdv-replica": replica},
                            gap,
                        )
                    if gap:
                        time.sleep(gap * cache_stream(*counts).count("\n\n"))
                    payload = cache_response(*counts)
                    if path == "/v1/messages":
                        forced_thinking(payload, body, fleet.forced_thinking_tokens)
                    return self._send(200, payload, extra_headers={"x-llmdv-replica": replica})

                if fleet.header_probe_disposition and path == "/v1/chat/completions":
                    value = self.headers.get("x-llmdv-example")
                    headers = (
                        {}
                        if fleet.header_probe_disposition == "stripped"
                        else {
                            "x-llmdv-example": "rewritten"
                            if fleet.header_probe_disposition == "rewritten"
                            else value
                        }
                    )
                    if fleet.header_probe_defect == "wrong":
                        headers = {"x-llmdv-example": "wrong"}
                    probe = {
                        "instrument": "llmdv.header_echo/1",
                        "probe_id": self.headers.get("x-llmdv-probe-id"),
                        "allowlist": ["x-llmdv-example"],
                        "headers": headers,
                    }
                    if fleet.header_probe_defect == "stale":
                        probe["probe_id"] = "old"
                    if fleet.header_probe_defect == "unconfigured":
                        probe["allowlist"] = []
                    return self._send(200, {"llmdv_header_probe": probe})
                answer = structured_answer(body, path, fleet.structured_output_defect)
                if answer is None and path == "/v1/messages":
                    answer = mid_system_answer(body, fleet.context_defect)
                if path == "/v1/responses":
                    refusal = response_input_refusal(body, fleet.response_config.input_defect)
                    if refusal is not None:
                        return self._send(
                            400, {"error": {"message": refusal, "type": "invalid_request_error"}}
                        )
                    payload = response_body(fleet.model)
                    instructions, given = response_input_view(
                        body, fleet.response_config.input_defect
                    )
                    if instructions is not None:
                        payload["instructions"] = instructions
                        if fleet.response_config.input_defect == "changes_instructions":
                            payload["instructions"] = "You are a helpful assistant."
                    if answer is None:
                        answer = code_word_answer(context, instructions, given)
                    if answer is None:
                        answer = replayed_answer(given)
                    if answer is None:
                        answer = leaked_answer(spoken_answer(body), fleet.answer_leak)
                    payload["output"][0]["content"][0]["text"] = answer
                    if fleet.abort_finish_reason:
                        payload.update(
                            status="failed",
                            error={"code": "server_error", "message": "generation aborted"},
                        )
                    if fleet.response_options:
                        response_option_payload(payload, body, fleet.response_option_defect)
                    response_shape(payload, body, fleet.response_config.shape_defect)
                    included = body.get("include") or []
                    if (
                        fleet.response_config.encrypted_reasoning
                        and "reasoning.encrypted_content" in included
                    ):
                        sealed = {"type": "reasoning", "id": "rs_sealed", "summary": []}
                        payload["output"].insert(0, {**sealed, "encrypted_content": "opaque"})
                    payload.update(fleet.response_overrides)
                    if body.get("stream"):
                        stream = fleet.responses_stream
                        if stream is None:
                            events = response_events(response=payload)
                            if fleet.response_option_defect == "stream_logprobs_dropped":
                                for event in events:
                                    event.pop("logprobs", None)
                            if fleet.response_config.stream_defect == "renamed_items":
                                for entry in events[-1]["response"]["output"]:
                                    entry["id"] = f"renamed_{entry['id']}"
                            stream = named_sse(events)
                        return self._named_stream(stream, fleet.responses_stream_content_type)
                    return self._send(200, payload)
                if path == "/v1/messages":
                    if body.get("stream"):
                        stream = fleet.messages_stream
                        if stream is None:
                            events = message_events(
                                fleet.model, "abort" if fleet.abort_finish_reason else "end_turn"
                            )
                            if answer is None:
                                answer = transport_fakes.requested_line(body)
                            if answer is not None:
                                parts = iter(
                                    (answer[: len(answer) // 2], answer[len(answer) // 2 :])
                                )
                                for event in events:
                                    if event["type"] == "content_block_delta":
                                        event["delta"]["text"] = next(parts, "")
                            stream = named_sse(events)
                        return self._named_stream(stream, fleet.messages_stream_content_type)
                    return self._send(
                        200,
                        {
                            "id": "msg_fake",
                            "type": "message",
                            "role": "assistant",
                            "model": fleet.model,
                            "content": [
                                {
                                    "type": "text",
                                    "text": answer
                                    if answer is not None
                                    else leaked_answer(spoken_answer(body), fleet.answer_leak),
                                }
                            ],
                            "stop_reason": "abort" if fleet.abort_finish_reason else "end_turn",
                            "stop_sequence": None,
                            "usage": {
                                "input_tokens": prompt_words(body, fleet.context_defect)
                                if fleet.prompt_accounting
                                else 3,
                                "output_tokens": (fleet.effort_tokens or {}).get(
                                    (body.get("output_config") or {}).get("effort"), 2
                                ),
                            },
                            **fleet.message_overrides,
                        },
                    )

                if body.get("stream"):
                    return self._stream(body)

                if path == "/v1/completions":
                    return self._send(
                        200,
                        {
                            "id": "cmpl-fake",
                            "object": "text_completion",
                            "model": fleet.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "text": "ok",
                                    "finish_reason": "abort"
                                    if fleet.abort_finish_reason
                                    else "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 3,
                                "completion_tokens": 1,
                                "total_tokens": 4,
                            },
                            **fleet.completion_overrides,
                        },
                    )

                content = answer
                if content is None:
                    content = leaked_answer(spoken_answer(body), fleet.answer_leak)
                if fleet.inline_thinking:
                    content = f"<think>\nweighing it up</think>{content}"
                return self._send(
                    200,
                    {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion",
                        "model": fleet.model,
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "abort" if fleet.abort_finish_reason else "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": content,
                                    "reasoning_content": None,
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                        **fleet.chat_overrides,
                    },
                )

        self._server = transport_fakes.QuietServer(("127.0.0.1", 0), Handler)
        if tls_cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls_cert)
            transport_fakes.tls_context(self, context)
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def prefix_cache(self, body: dict) -> tuple[int, int, str]:
        """Read, then write, whole prompt blocks per cache_salt, as a prefix-caching engine does.

        Returns the prompt length, the cached tokens reported and the replica that served it.
        The last prompt token is always recomputed, so a full hit stops a block short of it when
        the prompt is a whole number of blocks. Each `cache_defect` is a way a stack can lose or
        misreport that reuse.
        """
        defect, block = self.cache_defect, self.cache_block
        self.cache_requests += 1
        kwargs = body.get("chat_template_kwargs") or {}
        if "reasoning_effort" in kwargs and defect != "kwargs_unrendered":
            # The template reads the effort from a kwarg as from the top-level field.
            body = {**body, "reasoning_effort": kwargs["reasoning_effort"]}
        if defect == "tools_dropped_on_none" and body.get("tool_choice") in (
            "none",
            {"type": "none"},
        ):
            # A render that leaves the tools out when the client's choice is none.
            body = {key: value for key, value in body.items() if key != "tools"}
        tokens = cache_tokens(
            canonical_body(body, defect),
            canonical_tools=defect != "key_order_sensitive",
            reverse_tools=self.cache_requests % 2 == 0
            and (
                defect == "tools_unstable"
                # A render whose tool order is unstable only past 32 tools.
                or (defect == "many_tools_unstable" and len(body.get("tools") or []) > 32)
            ),
            hoist_system=defect == "hoist_system",
            merge_system=defect == "merge_system",
            image_blind=defect == "image_blind",
            tool_preamble=self.tool_preamble,
        )
        if defect == "layer_rewrites":
            # A layer that adds its own text ahead of the prompt it forwards.
            tokens = ["Forwarded", "by", "the", "gateway."] + tokens
        if defect in ("settings_apart", "kwargs_unrendered"):
            # A template that opens with the reasoning setting, in each API's own words. With
            # `kwargs_unrendered` the render reads it from top-level fields alone and leaves a
            # template kwarg out, as a router rendering the prompt itself can.
            head = [_text({name: body[name]}) for name in REASONING_FIELDS if name in body]
            tokens = " ".join(head).split() + tokens
        if defect == "budget_tiers":
            # Not a defect of the stack: a gateway that opens the prompt with one effort for
            # every API, mapping a Messages thinking budget onto an effort in tiers.
            tokens = [f"effort={tiered_effort(body)}"] + tokens
        # A layer that places a session by its first user turn moves a compacted session; one
        # that places it by its whole system prompt moves a client with a clock line.
        head = system_text(body) if defect == "system_keyed" else first_user_turn(body)
        moved = (
            defect in ("session_by_head", "system_keyed")
            and bool(self.cache_heads)
            and head not in self.cache_heads
        )
        self.cache_heads.add(head)
        # A layer that places a request by fields no template renders, such as sampling or the
        # end user, moves a repeat that changes only those.
        placement = json.dumps({name: body.get(name) for name in UNRENDERED_FIELDS}, sort_keys=True)
        moved = moved or (
            defect == "fields_keyed"
            and bool(self.cache_placements)
            and placement not in self.cache_placements
        )
        self.cache_placements.add(placement)
        overlapped = defect == "concurrent_cold" and self.overlaps_another()
        # `salt_lost_in_session` forwards the salt only until a request carries history.
        lost = defect == "salt_lost_in_session" and carries_history(body)
        salt = "" if defect == "ignored_salt" or lost else str(body.get("cache_salt") or "")
        if defect == "salt_flush" and salt and salt not in self.cache_store:
            self.cache_store.clear()
        spaces = (
            list(self.cache_store.values())
            if defect == "unsalted_leak" and not salt
            else [self.cache_store.get(salt, set())]
        )
        readable, hit = (len(tokens) - 1) // block, 0
        while hit < readable and any(tuple(tokens[: (hit + 1) * block]) in s for s in spaces):
            hit += 1
        if defect == "unchained_blocks":
            # Each block matched by its own content at its position, without the chain before it.
            hit = sum(
                any(
                    len(prefix) == (k + 1) * block
                    and prefix[k * block :] == tuple(tokens[k * block : (k + 1) * block])
                    for space in spaces
                    for prefix in space
                )
                for k in range(readable)
            )
        limit = body.get("max_output_tokens", body.get("max_tokens"))
        uncached = defect == "tiny_output_uncached" and limit == 1
        if not uncached:
            self.cache_store.setdefault(salt, set()).update(
                tuple(tokens[: k * block]) for k in range(1, len(tokens) // block + 1)
            )
        cached, replica = hit * block, "replica-1"
        if defect == "replica" and cached:
            # The repeat reached another replica, which holds none of the prefix.
            cached, replica = 0, "other"
        if (
            uncached
            or defect == "cold"
            or (defect == "collapse" and self.cache_requests == 3)
            or (defect == "reasoning_blind" and any(name in body for name in REASONING_FIELDS))
            # A layer that cannot read a conversation's history places every later turn blind.
            or (defect == "history_blind" and carries_history(body))
            or moved
            or overlapped
            # Prompts past a size are placed without prefix data, as a router that cannot
            # tokenize them does.
            or (defect == "long_prompt_blind" and len(tokens) > 192)
            # A router whose tokenizer disagrees with the engine's on other scripts, or that
            # cannot tokenize a very long line, finds no replica holding the prompt.
            or (defect == "non_ascii_blind" and not json.dumps(body, ensure_ascii=False).isascii())
            or (defect == "long_line_blind" and longest_line(body) > 64)
            # A render that fails on block-form Responses input places those sessions blind.
            or (defect == "input_blocks_blind" and has_input_blocks(body))
            # A layer that renders with the client's output budget against a tighter limit
            # fails to render a near-window request, and places it blind.
            or (
                defect == "render_with_budget"
                and isinstance(limit, int)
                and len(tokens) + limit > 0.9 * self.max_model_len
            )
        ):
            cached = 0
        if self.cache_withheld and cached:
            cached = max(cached - self.cache_withheld * block, 0)
        if defect == "short_block" and cached:
            cached -= block
        if defect == "stale_first":
            cached = readable * block
        return len(tokens), cached, replica

    def overlaps_another(self) -> bool:
        """Hold the request briefly; whether another arrived meanwhile.

        Models a layer that spills concurrent requests for one prefix to cold replicas.
        """
        with self.cache_lock:
            self.cache_inflight += 1
            overlapped = self.cache_inflight > 1
        time.sleep(0.3)
        with self.cache_lock:
            overlapped = overlapped or self.cache_inflight > 1
            self.cache_inflight -= 1
        return overlapped

    # ------------------------------------------------------------- lifecycle

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"{'https' if self.tls_cert else 'http'}://{host}:{port}"

    def __enter__(self) -> FakeFleet:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def profile_yaml(
    base_url: str,
    model: str = MODEL,
    responses: bool = True,
    unstructured_errors: tuple[str, ...] = (),
    surfaces: tuple[str, ...] = ("chat_completions", "completions", "messages"),
    max_model_len: int = MAX_MODEL_LEN,
    fleet_policy: str | None = None,
    rate_limited: bool = False,
    count_tokens: bool = True,
    structured_output: bool = False,
) -> str:
    """A profile describing one fake ingress, matching the fake's real behaviour."""
    policy = f"\nfleet_policy: {fleet_policy}" if fleet_policy else ""
    unstructured = (
        "\n    unstructured_errors: [" + ", ".join(unstructured_errors) + "]"
        if unstructured_errors
        else ""
    )
    return f"""
schema: llmdv.profile/1
name: fake
vantage: external{policy}
model:
  public_id: {model}
  max_model_len: {max_model_len}
  reasoning: {{parser: null, inline_tag: think, default_on: true}}
capabilities:
  count_tokens: {str(count_tokens).lower()}
  structured_output: {str(structured_output).lower()}
ingresses:
  - id: fake
    kind: vllm
    base_url: {base_url}
    auth: {{type: none}}
    health_path: /health
    surfaces: [{", ".join(surfaces)}{", responses" if responses else ""}]
    rate_limited: {str(rate_limited).lower()}
    error_status:
      unknown_model: [404]
      unknown_path: [404]
      wrong_method: [405]
      # Measured on the fake, as on a real vLLM: unknown top-level fields are ignored.
      unsupported_option: [200]{unstructured}
headers:
  x-request-id: {{fake: ignored}}
clients:
  - {{id: forge, surfaces: [chat_completions, messages], needs: [reasoning]}}
  - {{id: rollout-probe, surfaces: [completions], needs: []}}
"""


# --------------------------------------------------------------------- unattended runs


class _Server:
    """A threaded local HTTP server, started and stopped as a context manager."""

    handler: type[BaseHTTPRequestHandler]

    def _serve(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self._server = transport_fakes.QuietServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()


class FakeKubeAPI(_Server):
    """An absent pod with independently allowed or denied access to its log subresource.

    Discovery lets real kubectl exercise its initial Pod lookup for `logs`. A missing Pod
    returns 404 even when reading `pods/log` would return 403.
    """

    def __init__(self, *, allow_logs: bool) -> None:
        self.requests: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):  # noqa: N802 -- BaseHTTPRequestHandler API
                owner.requests.append(self.path)
                path = urlsplit(self.path).path
                code = 200
                if path == "/api":
                    body = {"kind": "APIVersions", "versions": ["v1"]}
                elif path == "/apis":
                    body = {"kind": "APIGroupList", "groups": []}
                elif path == "/api/v1":
                    body = {
                        "kind": "APIResourceList",
                        "groupVersion": "v1",
                        "resources": [
                            {"name": name, "namespaced": True, "kind": "Pod", "verbs": ["get"]}
                            for name in ("pods", "pods/log")
                        ],
                    }
                else:
                    denied = path.endswith("/log") and not allow_logs
                    code = 403 if denied else 404
                    body = {
                        "kind": "Status",
                        "status": "Failure",
                        "reason": "Forbidden" if denied else "NotFound",
                        "message": (
                            "pods/log is forbidden"
                            if denied
                            else 'pods "llmdv-access-probe" not found'
                        ),
                        "code": code,
                    }
                body["apiVersion"] = "v1"
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._serve(Handler)


class FakeS3(_Server):
    """Path-style S3 uploads, object reads and listings for published runs.

    `deny_after=N` accepts N uploads and then refuses every further one with 403, which boto3
    does not retry -- the shape of credentials that can write the preflight check object but
    lose access, or a bucket that fills, partway through publication.
    """

    def __init__(
        self,
        deny_after: int | None = None,
        deny_keys: tuple[str, ...] = (),
        deny_once: tuple[str, ...] = (),
        bearer_tokens: tuple[str, ...] = (),
        deny_message: str = "denied",
    ) -> None:
        self.deny_after = deny_after
        # Refuse any key ending with one of these, and accept the rest.
        self.deny_keys = deny_keys
        # Refuse only the first upload of a key ending with one of these.
        self.deny_once = set(deny_once)
        self.objects: dict[str, bytes] = {}
        self.order: list[str] = []
        self.headers: list[dict[str, str]] = []
        store = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: A002 - silence the default access log
                pass

            def _reply(self, code: int, body: bytes = b"") -> None:
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                if code == 200:
                    self.send_header("ETag", '"fake"')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlsplit(self.path)
                key = unquote(parsed.path).lstrip("/")
                query = parse_qs(parsed.query)
                if query.get("list-type") == ["2"]:
                    prefix = query.get("prefix", [""])[0]
                    bucket = key + "/"
                    keys = sorted(
                        k[len(bucket) :] for k in store.objects if k.startswith(bucket + prefix)
                    )
                    contents = "".join(f"<Contents><Key>{escape(k)}</Key></Contents>" for k in keys)
                    return self._reply(
                        200,
                        (
                            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                            f"<IsTruncated>false</IsTruncated>{contents}</ListBucketResult>"
                        ).encode(),
                    )
                if key not in store.objects:
                    return self._reply(404, b"<Error><Code>NoSuchKey</Code></Error>")
                return self._reply(200, store.objects[key])

            def do_PUT(self):
                length = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(length) if length else b""
                store.headers.append(dict(self.headers))
                if bearer_tokens and self.headers.get("Authorization") not in {
                    f"Bearer {token}" for token in bearer_tokens
                }:
                    return self._reply(
                        403, b"<Error><Code>AccessDenied</Code><Message>bad token</Message></Error>"
                    )
                key = self.path.split("?", 1)[0].lstrip("/")
                denied_key = any(key.endswith(suffix) for suffix in store.deny_keys)
                once = next((suffix for suffix in store.deny_once if key.endswith(suffix)), None)
                if once:
                    store.deny_once.discard(once)
                    denied_key = True
                if denied_key or (
                    store.deny_after is not None and len(store.order) >= store.deny_after
                ):
                    return self._reply(
                        403,
                        (
                            "<Error><Code>AccessDenied</Code>"
                            f"<Message>{escape(deny_message)}</Message></Error>"
                        ).encode(),
                    )
                store.objects[key] = data
                store.order.append(key)
                return self._reply(200)

        self._serve(Handler)


class FakeIAM(_Server):
    """Exchange a synthetic IBM API key or refresh token for a bearer token."""

    api_key = "llmdv-ibm-api-key"
    refresh_token = "llmdv-ibm-refresh-token"

    def __init__(self, *, reject: bool = False) -> None:
        self.received: list[dict[str, list[str]]] = []
        self.tokens: list[str] = []
        iam = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                form = parse_qs(raw)
                iam.received.append(form)
                valid = (
                    form.get("grant_type") == ["urn:ibm:params:oauth:grant-type:apikey"]
                    and form.get("apikey") == [iam.api_key]
                ) or (
                    form.get("grant_type") == ["refresh_token"]
                    and form.get("refresh_token") == [iam.refresh_token]
                )
                code = 200 if valid and not reject and self.path == "/identity/token" else 400
                if code == 200:
                    token = f"llmdv-ibm-access-token-{len(iam.tokens) + 1}"
                    iam.tokens.append(token)
                    body = {
                        "access_token": token,
                        "refresh_token": iam.refresh_token,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    }
                else:
                    # Echo credentials deliberately: callers must never leak provider errors.
                    body = {"errorCode": "invalid_credentials", "errorMessage": f"rejected: {raw}"}
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._serve(Handler)


class FakeSlack(_Server):
    """A Slack incoming webhook that answers from a script, then with 200 "ok"."""

    def __init__(self, script: list[tuple[int, dict[str, str]]] | None = None) -> None:
        self.script = list(script or [])
        self.received: list[dict] = []
        hook = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: A002 - silence the default access log
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                hook.received.append(json.loads(self.rfile.read(length) or b"{}"))
                code, headers = hook.script.pop(0) if hook.script else (200, {})
                body = b"ok" if code == 200 else b"rate_limited"
                self.send_response(code)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._serve(Handler)


SRC = Path(__file__).resolve().parents[1] / "src"


def s3_env(store: FakeS3) -> dict[str, str]:
    """Environment pointing boto3 at a fake store, with no credentials from the real machine."""
    return {
        "LLMDV_S3_AUTH": "aws",
        "AWS_ENDPOINT_URL": store.base_url,
        "AWS_ACCESS_KEY_ID": "llmdv-test",
        "AWS_SECRET_ACCESS_KEY": "llmdv-test-secret",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_CONFIG_FILE": "/nonexistent",
        "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent",
    }


def ibm_cos_env(store: FakeS3, iam: FakeIAM) -> dict[str, str]:
    """IBM IAM only; AWS discovery is disabled and both services are local fakes."""
    return {
        "LLMDV_S3_AUTH": "ibm-iam",
        "IBM_COS_API_KEY": iam.api_key,
        "IBM_COS_SERVICE_INSTANCE_ID": "llmdv-test-instance",
        "IBM_COS_ENDPOINT": store.base_url,
        "IBM_COS_AUTH_ENDPOINT": f"{iam.base_url}/identity/token",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": "/nonexistent",
        "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent",
    }


def run_llmdv(
    *args: str, env: dict[str, str] | None = None, **popen
) -> subprocess.CompletedProcess:
    """Run the real `llmdv` CLI in a subprocess, isolated from the caller's environment."""
    return subprocess.run(  # noqa: S603 - fixed interpreter and args
        [sys.executable, "-m", "llmdv.cli.main", *args],
        capture_output=True,
        text=True,
        cwd=SRC.parent,
        env={
            "PYTHONPATH": str(SRC),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            **(env or {}),
        },
        **popen,
    )


def vllm_option_payload(body, path, model, defect):
    prompt = body.get("input") if path == "/v1/responses" else body["messages"][0]["content"]
    inputs, output, finish = len(prompt.split()), 2, "stop"
    if defect != "ignored":
        if body.get("truncate_prompt_tokens"):
            inputs = min(inputs, body["truncate_prompt_tokens"])
        output = max(output, body.get("min_tokens", 0))
        if body.get("ignore_eos"):
            output, finish = body.get("max_tokens", body.get("max_output_tokens")), "length"
    if path == "/v1/responses":
        payload = response_body(model)
        payload["usage"] = {
            "input_tokens": inputs,
            "output_tokens": output,
            "total_tokens": inputs + output,
        }
        if finish == "length":
            payload.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    else:
        payload = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": finish,
                }
            ],
            "usage": {
                "prompt_tokens": inputs,
                "completion_tokens": output,
                "total_tokens": inputs + output,
            },
        }
        if body.get("return_token_ids") and defect != "ignored":
            payload["prompt_token_ids"] = list(range(inputs))
            payload["choices"][0]["token_ids"] = (
                [True] * output if defect == "bool_ids" else list(range(output))
            )
    return payload


def response_tool_payload(body, model, defect=None):
    payload = response_body(model)
    inputs = body.get("input")
    if isinstance(inputs, list):
        results = [item for item in inputs if item.get("type") == "function_call_output"]
        result = results[0] if results else {}
        text = result.get("output") if result.get("call_id") == "call_fake" else "wrong call_id"
        payload["output"][0]["content"][0]["text"] = (
            "ignored"
            if defect == "ignores_result"
            else f"The result is {text}."
            if defect == "prose_result"
            else text
        )
        return payload
    marker = body["tools"][0]["parameters"]["properties"]["marker"]["enum"][0]
    arguments = {"marker": "wrong" if defect == "schema" else marker}
    payload["output"] = [
        {
            "id": "fc_fake",
            "type": "function_call",
            "call_id": "call_fake",
            "name": "llmdv_echo",
            "arguments": json.dumps(arguments),
            "status": "completed",
        }
    ]
    item = payload["output"][0]
    if defect == "argument_type":
        item["arguments"] = arguments
    elif defect == "json":
        item["arguments"] = "{broken"
    elif defect == "id":
        item["call_id"] = ""
    elif defect == "name":
        item["name"] = "other"
    elif defect == "abort":
        payload.update(status="failed", error={"code": "generation_aborted"})
    return payload


def response_tool_events(payload, defect=None):
    item = deepcopy(payload["output"][0])
    if item.get("type") != "function_call":
        return response_events(response=payload)
    start = {**payload, "status": "in_progress", "output": [], "usage": None, "error": None}
    events = [
        {"type": "response.created", "response": start},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "arguments": "", "status": "in_progress"},
        },
    ]
    arguments = item["arguments"]
    fragments = [arguments[:5], arguments[5:]] if isinstance(arguments, str) else [arguments]
    if defect == "dropped_fragment":
        fragments = fragments[1:]
    address = {"output_index": 0, "item_id": "fc_fake"}
    events.extend(
        {"type": "response.function_call_arguments.delta", **address, "delta": part}
        for part in fragments
    )
    events.append(
        {"type": "response.function_call_arguments.done", **address, "arguments": arguments}
    )
    if defect == "changed_call_id":
        item["call_id"] = "call_other"
    if defect == "stale_item":
        item["arguments"] = "{}"
    events.append({"type": "response.output_item.done", "output_index": 0, "item": item})
    final = deepcopy(payload)
    if defect == "stale_terminal":
        final["output"][0]["arguments"] = "{}"
    events.append(
        {
            "type": "response.failed" if defect == "abort" else "response.completed",
            "response": final,
        }
    )
    return [{**event, "sequence_number": index} for index, event in enumerate(events)]


def render_payload(body, path, model, defect, call):
    prompt = body.get("prompt", body.get("input", body.get("messages")))
    parts = [body.get("system", "")]
    if isinstance(prompt, list):
        parts.extend(item.get("content", "") for item in prompt)
    else:
        parts.append(prompt)
    if body.get("tools"):
        parts.insert(0, json.dumps(body["tools"], sort_keys=defect == "sorted_tools"))
    serialized = "|".join(str(part) for part in parts) + "<end>"
    if defect == "long_template":
        serialized += "<assistant><think></think>" * 4
    tokens = [ord(char) for char in serialized]
    if defect in ("hoisted", "mid_prefix"):
        volatile = "transient-bbb" if "transient-bbb" in serialized else "transient-aaa"
        position = 0 if defect == "hoisted" else 64
        tokens[position:position] = [ord(char) for char in volatile]
    if defect == "constant":
        tokens = [1, 2, 3]
    elif defect == "unstable":
        tokens.append(call)
    elif defect == "boolean":
        tokens = [True]
    elif defect == "empty":
        tokens = []
    elif defect == "substituted":
        tokens[len(tokens) // 2] += 1
    result = {
        "request_id": f"render-{call}",
        "model": "other" if defect == "model" else model,
        "token_ids": tokens,
        "sampling_params": {"max_tokens": 8},
    }
    if path == "/v1/completions/render" and defect != "batch_shape":
        return [result]
    return result


# Claude Code's recovery from a failed generation, for the `fallback` defect: a streaming
# request that fails is sent again without streaming -- the same generation a second time --
# and a second failure ends the run with an error result naming the status, exit code 1.
CLIENT_FALLBACK = r"""import json, sys, urllib.error, urllib.request
def fallback(req):
    body = json.loads(req.data)
    for stream in (True, False):
        attempt = urllib.request.Request(req.full_url,
            data=json.dumps(dict(body, stream=stream)).encode(), headers=dict(req.header_items()))
        try:
            with urllib.request.urlopen(attempt) as response:
                return response.read(), stream
        except urllib.error.HTTPError as error:
            failure = error
            detail = error.read().decode()
    sys.stdout.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': True,
        'api_error_status': failure.code, 'terminal_reason': 'api_error', 'modelUsage': {},
        'result': 'API Error: %d %s' % (failure.code, detail)}) + '\n')
    raise SystemExit(1)
"""


def fake_claude_binary(path, version="2.1.0", defect=None):
    import textwrap

    source = r"""import json, os, sys, urllib.request, urllib.error
if '--version' in sys.argv:
    sys.stdout.write(VERSION + ' (Claude Code)\n'); raise SystemExit
if DEFECT == 'timeout':
    import time; time.sleep(30)
if DEFECT == 'huge':
    sys.stdout.write('x' * 300000); raise SystemExit
model = os.environ['ANTHROPIC_MODEL']
assert 'ANTHROPIC_AUTH_TOKEN' not in os.environ
assert 'NODE_OPTIONS' not in os.environ
assert sys.argv[sys.argv.index('--tools') + 1] == ''
# Claude Code sizes every request by its output budget.
budget = int(os.environ['CLAUDE_CODE_MAX_OUTPUT_TOKENS'])
body = {'model': model, 'messages': [{'role': 'user', 'content': sys.argv[-1]}],
        'max_tokens': budget}
session = None
if '--session-id' in sys.argv or '--resume' in sys.argv:
    from pathlib import Path
    session = sys.argv[sys.argv.index('--resume' if '--resume' in sys.argv else '--session-id') + 1]
    state = Path(os.environ['HOME']) / (session + '.json')
    if '--resume' in sys.argv and DEFECT != 'lost_history':
        previous = json.loads(state.read_text())
        body['messages'] = previous + [{'role': 'assistant', 'content': 'ok'}] + body['messages']
    state.write_text(json.dumps(body['messages']))
if DEFECT == 'stream': body['stream'] = True
if DEFECT == 'output_limit': body['max_tokens'] = 9999
req = urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages',
    data=json.dumps(body).encode(),
    headers={'Content-Type': 'application/json', 'anthropic-version': '2023-06-01'})
if DEFECT == 'fallback':
    payload, body['stream'] = fallback(req)
else:
    with urllib.request.urlopen(req) as response:
        payload = response.read()
if DEFECT == 'retry':
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError:
        pass
result = {'type': 'result', 'subtype': 'success', 'is_error': False,
          'result': 'ok', 'modelUsage': {model: {}}}
if not body.get('stream'):
    reply = json.loads(payload)
    if not any(part.get('type') == 'text' for part in reply.get('content') or []):
        # A turn that ends without text asks for another turn, which --max-turns 1 refuses.
        result = {'type': 'result', 'subtype': 'error_max_turns', 'is_error': True,
                  'modelUsage': {model: {}}}
if session: result['session_id'] = session
if DEFECT == 'error': result['is_error'] = True
if DEFECT == 'wrong_model': result['modelUsage'] = {'other': {}}
if DEFECT == 'empty': result['result'] = ''
sys.stdout.write(json.dumps(result))
if result['subtype'] == 'error_max_turns': raise SystemExit(1)
"""
    path.write_text(
        f"#!{sys.executable}\nVERSION = {version!r}\nDEFECT = {defect!r}\n"
        + CLIENT_FALLBACK
        + textwrap.dedent(source)
    )
    path.chmod(0o700)
    return path


REASONING_FIELDS = ("reasoning_effort", "thinking", "reasoning", "output_config")


def tiered_effort(body: dict) -> str | None:
    """The effort a request renders as, Messages budgets in tiers as measured behind LiteLLM.

    A budget below 2048 renders as low, below 4096 as medium, and anything larger as high.
    """
    if "reasoning_effort" in body:
        return body["reasoning_effort"]
    if isinstance(body.get("reasoning"), dict):
        return body["reasoning"].get("effort")
    thinking = body.get("thinking") or {}
    if thinking.get("type") == "enabled":
        budget = thinking.get("budget_tokens", 0)
        return "low" if budget < 2048 else "medium" if budget < 4096 else "high"
    if thinking.get("type") == "adaptive":
        return (body.get("output_config") or {}).get("effort", "high")
    return "none" if thinking else None


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def cache_tokens(
    body: dict,
    *,
    canonical_tools: bool,
    reverse_tools: bool,
    hoist_system: bool = False,
    merge_system: bool = False,
    image_blind: bool = False,
    tool_preamble: int = 0,
) -> list[str]:
    """The fake's rendered prompt as whitespace tokens.

    Tools first, then the conversation, then any reasoning setting, the order a chat template
    commonly renders them in. A canonical render sorts each tool's keys. `hoist_system` renders
    the system entries inside the history ahead of everything, as a hoisting template does.
    An image is one token, its data, unless the cache is `image_blind` and keys the prompt on
    its text alone. With a `tool_preamble`, the leading system message comes first, then that
    many fixed words of tool instructions, then the tools, as a template with a fixed tools
    section renders them.
    """
    if image_blind:
        body = json.loads(re.sub(r"[A-Za-z0-9+/=]{200,}", "image", json.dumps(body)))
    hoisted: list[dict] = []
    if hoist_system or merge_system:
        hoisted, body = split_inline_system(body)
    if merge_system and hoisted:
        # Appended to the leading system message, as a render that merges them does.
        messages = [dict(m) for m in body.get("messages") or []]
        notes = " ".join(str(block_text(m.get("content"))) for m in hoisted)
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = f"{block_text(messages[0].get('content'))} {notes}"
        body, hoisted = {**body, "messages": messages}, []
    tools = list(body.get("tools") or [])
    if reverse_tools:
        tools.reverse()
    parts = [_text(hoisted)] if hoisted else []
    messages = list(body.get("messages") or [])
    if tools and tool_preamble:
        lead = messages[:1] if messages and messages[0].get("role") == "system" else []
        messages = messages[len(lead) :]
        for message in lead:
            parts += [str(message.get("role")), _text(message.get("content"))]
        parts += [f"tool-rule-{index}" for index in range(tool_preamble)]
    parts += [json.dumps(tools, sort_keys=canonical_tools)] if tools else []
    parts += [_text(body[name]) for name in ("system", "instructions", "input") if name in body]
    for message in messages:
        parts += [str(message.get("role")), _text(message.get("content"))]
    parts += [_text({name: body[name]}) for name in REASONING_FIELDS if name in body]
    return " ".join(parts).split()


# A leading system line the fake strips before rendering, as a stack declaring
# `stripped_system_prefix` does; the `attribution_kept` defect renders it.
ATTRIBUTION_PREFIX = "x-llmdv-attribution:"
CACHE_HINTS = ("prompt_cache_key", "prompt_cache_retention", "prompt_cache_options")
# Request fields no template renders: sampling and the end user a client names.
UNRENDERED_FIELDS = ("temperature", "top_p", "user", "metadata", "safety_identifier")
TEXT_BLOCKS = ("text", "input_text", "output_text")


def block_text(content):
    """Text blocks joined as a template renders them; any other content unchanged."""
    if isinstance(content, list) and all(
        isinstance(block, dict) and block.get("type") in TEXT_BLOCKS for block in content
    ):
        return "\n\n".join(str(block.get("text")) for block in content)
    return content


def canonical_body(body: dict, defect: str | None) -> dict:
    """Any surface's request as one Chat-shaped message list, as an engine renders each API.

    Messages `system` blocks and Responses `instructions` become the leading system message and
    text blocks become their text, so one conversation renders alike on every API. A leading
    attribution line is dropped, and cache hints and cache markers are not rendered. Each
    defect named here undoes one of those.
    """
    if defect == "messages_render_differs" and "system" in body:
        return body
    system = body.get("system", body.get("instructions"))
    entries = body.get("input", body.get("messages")) or []
    if isinstance(entries, str):
        entries = [{"role": "user", "content": entries}]
    messages = [] if system is None else [{"role": "system", "content": block_text(system)}]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role") or entry.get("type")
        content = entry.get("content") if "role" in entry else entry
        messages.append({"role": role, "content": block_text(content)})
    if messages and messages[0]["role"] == "system" and defect != "attribution_kept":
        text = messages[0]["content"]
        if isinstance(text, str) and text.startswith(ATTRIBUTION_PREFIX):
            messages[0]["content"] = text.partition("\n\n")[2]
    if defect == "hints_rendered":
        markers = [b.get("cache_control") for b in body.get("system") or [] if isinstance(b, dict)]
        hints = {**{k: body[k] for k in CACHE_HINTS if k in body}, "markers": markers}
        messages.insert(0, {"role": "hints", "content": hints})
    rest = {k: v for k, v in body.items() if k not in ("system", "instructions", "input")}
    return {**rest, "messages": messages}


def split_inline_system(body: dict) -> tuple[list[dict], dict]:
    """The system entries inside a conversation's history, and the body without them."""
    body, inline = dict(body), []
    for key in ("messages", "input"):
        entries = body.get(key)
        if not isinstance(entries, list):
            continue
        kept = []
        for index, entry in enumerate(entries):
            # Chat's own system prompt is its first message; Messages keeps it in `system`.
            leading = key == "messages" and index == 0 and "system" not in body
            if isinstance(entry, dict) and entry.get("role") == "system" and not leading:
                inline.append(entry)
            else:
                kept.append(entry)
        body[key] = kept
    return inline, body


def system_text(body: dict) -> str:
    """The system prompt as rendered, in any surface's shape."""
    messages = canonical_body(body, None)["messages"]
    return _text(messages[0]["content"]) if messages and messages[0]["role"] == "system" else ""


def first_user_turn(body: dict) -> str:
    entries = [*(body.get("messages") or []), *(body.get("input") or [])]
    users = [e for e in entries if isinstance(e, dict) and e.get("role") == "user"]
    return _text(users[0].get("content")) if users else ""


def has_blank_turn(body: dict) -> bool:
    """Whether any history entry's text is empty or whitespace only."""
    entries = [*(body.get("messages") or []), *(body.get("input") or [])]
    for entry in entries:
        if not isinstance(entry, dict) or "role" not in entry:
            continue
        text = block_text(entry.get("content"))
        if isinstance(text, str) and not text.strip():
            return True
    return False


def longest_line(body: dict) -> int:
    """The most words on one line of any text the request carries."""
    texts: list[str] = []

    def walk(value) -> None:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(body)
    return max((len(line.split()) for text in texts for line in text.splitlines()), default=0)


def has_input_blocks(body: dict) -> bool:
    """Whether a Responses input item carries its text as `input_text` blocks."""
    return any(
        isinstance(item, dict)
        and isinstance(item.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "input_text" for b in item["content"])
        for item in body.get("input") or []
    )


def carries_history(body: dict) -> bool:
    """Whether a request holds an earlier assistant turn, in any surface's shape."""
    entries = [*(body.get("messages") or []), *(body.get("input") or [])]
    return any(
        isinstance(entry, dict)
        and (entry.get("role") == "assistant" or entry.get("type") == "function_call")
        for entry in entries
    )


def cache_response(path, model, body, prompt, cached, defect):
    """A generation reporting the fake cache's counts in each surface's own usage shape.

    A one-token limit produces no answer, as a reasoning model's first token often is not one.
    On Responses, `budget_spent` is a healthy reply that reasons to `max_output_tokens` and
    ends incomplete, and `generation_failed` a generation that ends failed.
    """
    tiny = body.get("max_output_tokens", body.get("max_tokens")) == 1
    output = 1 if tiny else 2
    if defect == "impossible":
        cached = prompt + 1
    if path == "/v1/chat/completions":
        usage = {
            "prompt_tokens": prompt,
            "completion_tokens": output,
            "total_tokens": prompt + output,
        }
        if defect != "missing":
            usage["prompt_tokens_details"] = {"cached_tokens": cached}
        # The explicit null and empty fields an engine returns, which an SDK passes back.
        message = {
            "role": "assistant",
            "content": "" if tiny else "ok",
            "refusal": None,
            "tool_calls": [],
        }
        return {
            "id": "chatcmpl-cache",
            "object": "chat.completion",
            "model": model,
            "usage": usage,
            "choices": [
                {"index": 0, "message": message, "finish_reason": "length" if tiny else "stop"}
            ],
        }
    if path == "/v1/responses":
        payload = response_body(model)
        if tiny:
            payload.update(
                status="incomplete", incomplete_details={"reason": "max_output_tokens"}, output=[]
            )
        elif defect == "budget_spent":
            payload.update(reasoning_only_response())
        elif defect == "generation_failed":
            error = {"code": "server_error", "message": "generation failed"}
            payload.update(status="failed", error=error, output=[])
        payload["usage"] = {
            "input_tokens": prompt,
            "output_tokens": output,
            "total_tokens": prompt + output,
        }
        if defect != "missing":
            payload["usage"]["input_tokens_details"] = {"cached_tokens": cached}
        return payload
    read = -1 if defect == "impossible" else cached
    usage = {
        "input_tokens": prompt - max(read, 0),
        "output_tokens": output,
        "cache_creation_input_tokens": 0,
    }
    if defect != "missing":
        usage["cache_read_input_tokens"] = read
    return {
        "id": "msg_cache",
        "type": "message",
        "role": "assistant",
        "model": model,
        "stop_reason": "max_tokens" if tiny else "end_turn",
        "content": [] if tiny else [{"type": "text", "text": "ok"}],
        "usage": usage,
    }


def forced_thinking(payload: dict, body: dict, tokens: int) -> None:
    """Think `tokens` tokens before the answer, as a model that cannot turn thinking off does.

    A `max_tokens` that the thinking uses up ends the reply there: thinking only, no text, and
    `stop_reason: max_tokens`.
    """
    if not tokens:
        return
    limit = body.get("max_tokens")
    answer = payload["content"]
    thinking = {"type": "thinking", "thinking": "think " * tokens, "signature": "sig-fake"}
    payload["content"] = [thinking]
    if limit <= tokens:
        payload["stop_reason"] = "max_tokens"
        payload["usage"]["output_tokens"] = limit
        return
    payload["content"].extend(answer)
    payload["usage"]["output_tokens"] += tokens


def fake_claude_session_binary(path, version="2.1.154", defect=None):
    """A stand-in for a tool-using session: one relayed generation, then a stream-json trace.

    It reads the project the case prepared and reports what a working client would, so each
    defect removes exactly the behaviour one case exists to catch.
    """
    import textwrap

    source = r"""import json, os, re, sys, urllib.request
from pathlib import Path
if '--version' in sys.argv:
    sys.stdout.write(VERSION + ' (Claude Code)\n'); raise SystemExit
prompt = sys.argv[sys.argv.index('--print') + 1]
allowed = sys.argv[sys.argv.index('--allowedTools') + 1:]
def values(flag):
    # Variadic, so the list runs to the next flag rather than to one named successor.
    if flag not in sys.argv:
        return []
    rest = sys.argv[sys.argv.index(flag) + 1:]
    out = []
    for item in rest:
        if item.startswith('--'):
            break
        out.append(item)
    return out
def prompt_tools():
    return values('--tools')
if 'part1.txt' in prompt:
    assert '--tools' not in sys.argv
else:
    tools = prompt_tools()
    assert {name.split('(')[0] for name in allowed} <= set(tools), tools
# File writes are only ever granted for the project directory.
assert not {'Edit', 'Write'} & set(allowed), allowed
assert ('Edit(./**)' in allowed) == ('Edit' in prompt_tools()), allowed
# The project settings hold hooks, which are commands; a session must not be able to edit them.
assert values('--disallowedTools') == ['Edit(./.claude/**)'], sys.argv
assert sys.argv[sys.argv.index('--setting-sources') + 1] == 'project'
assert 'MAX_THINKING_TOKENS' not in os.environ
model = os.environ['ANTHROPIC_MODEL']
body = {'model': model, 'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': int(os.environ['CLAUDE_CODE_MAX_OUTPUT_TOKENS'])}
req = urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages',
    data=json.dumps(body).encode(),
    headers={'Content-Type': 'application/json', 'anthropic-version': '2023-06-01'})
if DEFECT == 'fallback':
    sys.stdout.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
    payload, _ = fallback(req)
else:
    urllib.request.urlopen(req).read()
events, answer = [], 'done'
def call(name, message='msg_1', parent=None, **inputs):
    events.append({'type': 'assistant', 'parent_tool_use_id': parent, 'message': {
        'id': message, 'content': [{'type': 'tool_use', 'id': 'toolu_' + str(len(events)),
                                    'name': name, 'input': inputs}]}})
if 'release notes' in prompt:
    if DEFECT != 'no_skill':
        call('Skill', skill='release-notes')
    answer = 'FORMAT-ID demo'
elif 'build id' in prompt:
    command = json.loads(Path('.claude/settings.json').read_text())
    command = command['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
    variable = re.search(r'\$(\w+)', command).group(1)
    answer = 'unknown' if DEFECT == 'no_hook' else os.environ[variable]
elif 'most lines' in prompt:
    assert allowed == ['Read']
    for index, name in enumerate(('a.txt', 'b.txt', 'c.txt')):
        turn = 'msg_%d' % index if DEFECT == 'serial_reads' else 'msg_1'
        call('Read', message=turn, file_path=name)
    answer = max(('a.txt', 'b.txt', 'c.txt'), key=lambda n: len(Path(n).read_text().splitlines()))
elif 'zorblax_handler' in prompt:
    call('Agent', description='find it')
    if DEFECT != 'flat_subagent':
        call('Agent', message='msg_2', parent='toolu_0', description='explore')
    answer = 'src/gamma.py'
elif 'part1.txt' in prompt:
    window = int(os.environ['CLAUDE_CODE_AUTO_COMPACT_WINDOW'])
    # A window at or above the model's own context can never be reached.
    assert 0 < window < int(os.environ.get('LLMDV_FAKE_CONTEXT', '10000000')), window
    parts = [Path('part%d.txt' % n).read_text() for n in range(1, 7)]
    codes = [re.search(r'CODE=(\w+)', part).group(1) for part in parts]
    if DEFECT != 'no_compaction':
        events.append({'type': 'system', 'subtype': 'compact_boundary'})
    answer = ' '.join(codes[:1] if DEFECT == 'forgets' else codes)
elif 'stripes.png' in prompt:
    assert allowed == ['Read']
    call('Read', file_path='stripes.png')
    import struct, zlib
    png = Path('stripes.png').read_bytes()
    width = struct.unpack('>I', png[16:20])[0]
    size = struct.unpack('>I', png[33:37])[0]
    row = zlib.decompress(png[41:41 + size])[1:1 + width * 3]
    palette = {(255, 0, 0): 'red', (0, 160, 0): 'green', (0, 0, 255): 'blue',
               (255, 255, 0): 'yellow', (0, 0, 0): 'black'}
    names = []
    for start in range(0, len(row), 3):
        name = palette[tuple(row[start:start + 3])]
        if name not in names[-1:]:
            names.append(name)
    answer = 'I cannot view images.' if DEFECT == 'blind' else ', '.join(names)
elif 'unittest' in prompt:
    fixes = {
        'shop/money.py': ('int(amount * 100)', 'round(amount * 100)'),
        'shop/inventory.py': ('self.available(sku) + 1:', 'self.available(sku):'),
        'shop/discounts.py': ('subtotal_cents > minimum', 'subtotal_cents >= minimum'),
        'shop/cart.py': ('sum(price for _sku, price, _qty in self.lines)',
                         'sum(price * qty for _sku, price, qty in self.lines)'),
        'shop/report.py': ('line[1] * line[2])', 'line[1] * line[2], reverse=True)'),
    }
    if DEFECT == 'edits_tests':
        Path('tests/test_shop.py').write_text('import unittest\n')
    elif DEFECT != 'unfixed':
        for name, (old, new) in fixes.items():
            text = Path(name).read_text()
            assert old in text, name
            Path(name).write_text(text.replace(old, new))
            call('Edit', message='msg_' + name, file_path=name)
for event in events:
    sys.stdout.write(json.dumps(event) + '\n')
result = {'type': 'result', 'subtype': 'success', 'is_error': DEFECT == 'error',
          'num_turns': len(events) + 1, 'result': answer, 'modelUsage': {model: {}}}
sys.stdout.write(json.dumps(result) + '\n')
"""
    path.write_text(
        f"#!{sys.executable}\nVERSION = {version!r}\nDEFECT = {defect!r}\n"
        + CLIENT_FALLBACK
        + textwrap.dedent(source)
    )
    path.chmod(0o700)
    return path


# The efforts a template takes as themselves under `kwargs_skip_effort_map`, whose deployment maps
# none and minimal onto low and max onto xhigh on the top-level field only.
TEMPLATE_EFFORTS = ("low", "medium", "high", "xhigh")
# A budget above this decodes at `long_decode_gap_s` per frame; calibration and probes stay fast.
LONG_DECODE_BUDGET = 64


def cache_stream(path, model, body, prompt, cached, defect):
    """The same counts streamed, or with `stream_cache_missing` not where clients read them.

    `no_terminal` ends a Responses stream after its last item, with no terminal event.

    `stream_cache_in_delta` is the gateway's healthy Messages shape: message_start carries the
    whole prompt as input_tokens with no cache fields, and the final message_delta carries the
    uncached input_tokens with both cache fields, which an SDK copies into the final message.
    """
    hidden = defect == "stream_cache_missing"
    in_delta = defect == "stream_cache_in_delta"
    shaped = None if hidden or in_delta else defect
    payload = cache_response(path, model, body, prompt, cached, shaped)
    usage = payload["usage"]
    if path == "/v1/chat/completions":
        if hidden:
            del usage["prompt_tokens_details"]
        frame = {"id": "chatcmpl-cache", "object": "chat.completion.chunk", "model": model}
        frames = [
            {**frame, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}]},
            {**frame, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        if (body.get("stream_options") or {}).get("include_usage"):
            frames.append({**frame, "choices": [], "usage": usage})
        return "".join(f"data: {json.dumps(item)}\n\n" for item in frames) + "data: [DONE]\n\n"
    if path == "/v1/responses":
        if hidden:
            del usage["input_tokens_details"]
        events = response_events(model, response=payload)
        return named_sse(events[:-1] if defect == "no_terminal" else events)
    events = message_events(model)
    events[0]["message"]["usage"] = usage
    if hidden:
        # Reported only at the end: message_start shows a cold prompt, the final delta the read.
        events[0]["message"]["usage"] = {
            **usage,
            "input_tokens": prompt,
            "cache_read_input_tokens": 0,
        }
        events[-2]["usage"]["cache_read_input_tokens"] = usage["cache_read_input_tokens"]
    if in_delta:
        events[0]["message"]["usage"] = {"input_tokens": prompt, "output_tokens": 0}
        events[-2]["usage"].update(
            {key: usage[key] for key in usage if key.endswith("input_tokens")}
        )
    return named_sse(events)


FUTURE_MARKERS = {
    "field": b"llmdv_future_field",
    "block": b"llmdv_future_block",
    "tool": b"llmdv_future_tool",
    "item": b"llmdv_future_item",
    "value": b"llmdv_future_value",
}


def forward_verdict(fleet, raw: bytes, headers, path: str) -> tuple[int, dict] | str | None:
    """What a fleet with `forward_defect` does with an unknown request element.

    `rejects_field` refuses a field inside a conversation entry, as a stricter release does;
    `block_5xx`, `tool_5xx`, `beta_5xx`, `item_5xx` and `value_5xx` answer a server error;
    `header_502` is a proxy that refuses an unknown header; `drops_connection` closes without a
    response; `accepts_tool` serves a request as if its unknown tool were absent; `crashes`
    serves the request and fails every later one, as an engine that died on it would;
    `count_5xx` serves an unknown block on Messages and answers a server error to the same block
    on count_tokens; `foreign_envelope` refuses an unknown tool with a 400 whose body is a
    framework default with no error envelope.
    """
    if fleet.crashed:
        return 503, {"error": {"message": "engine is dead", "type": "server_error"}}
    found = {name for name, marker in FUTURE_MARKERS.items() if marker in raw}
    if headers.get("x-llmdv-future-header"):
        found.add("header")
    if "llmdv-future" in (headers.get("anthropic-beta") or ""):
        found.add("beta")
    defect = fleet.forward_defect
    if not found:
        return None
    if defect == "crashes":
        fleet.crashed = True
        return None
    if defect == "drops_connection":
        return "drop"
    if defect == "accepts_tool" and "tool" in found:
        return "strip_tools"
    server = {
        "block_5xx": "block",
        "tool_5xx": "tool",
        "beta_5xx": "beta",
        "item_5xx": "item",
        "value_5xx": "value",
    }
    if server.get(defect) in found or (
        defect == "count_5xx" and "block" in found and path == COUNT_TOKENS_PATH
    ):
        return 500, {"error": {"message": "internal error", "type": "server_error"}}
    if defect == "header_502" and "header" in found:
        return 502, {"error": {"message": "bad gateway", "type": "server_error"}}
    if defect == "foreign_envelope" and "tool" in found:
        return 400, {"detail": "unknown tool type"}
    if defect == "rejects_field" and "field" in found:
        return 400, {
            "error": {"message": "Extra inputs are not permitted", "type": "invalid_request_error"}
        }
    return None


def rendered_size(raw: bytes, defect: str | None) -> int:
    """A synthetic input size computed from the bytes a hop delivered to the engine.

    `float64` re-encodes integers above 2^53 as floats, as a hop decoding into generic values
    does; `escape_mangle` turns each half of an escaped surrogate pair into U+FFFD;
    `drops_system` loses system content, as a lossy translation does; `template_line` adds a
    per-request line at the head of the prompt, as a changed template can.
    `sorted_keys` leaves the size alone and sorts the answer's keys in `sort_answer_keys`.
    """
    if defect == "escape_mangle":
        raw = re.sub(rb"\\ud[89a-f][0-9a-f]{2}", rb"\\ufffd", raw)
    body = json.loads(raw)
    if defect == "drops_system":
        body.pop("system", None)
        body.pop("instructions", None)
        if isinstance(body.get("messages"), list):
            body["messages"] = [m for m in body["messages"] if m.get("role") != "system"]
    if defect == "template_line":
        body["system"] = f"billing: {len(raw)}; {body.get('system') or ''}"
    if defect == "float64":
        body = json.loads(
            json.dumps(body), parse_int=lambda s: float(s) if abs(int(s)) > 2**53 else int(s)
        )
    keys = ("system", "instructions", "messages", "input", "prompt", "tools")
    return len(json.dumps([body.get(key) for key in keys], ensure_ascii=False))


# Last words that leave a raw prompt's sentence open, so a base continuation has text to write.
OPEN_ENDINGS = frozenset({"is", "are", "was", "were", "the", "a", "an", "of", "to", "and"})


def cueless(prompt) -> bool:
    """Whether a raw prompt leaves nothing to continue: an instruction or a finished sentence."""
    words = prompt.split() if isinstance(prompt, str) else []
    return bool(words) and words[-1].lower() not in OPEN_ENDINGS


def end_at_once(payload: dict) -> None:
    """Rewrite a Completions reply as one that ended at once: no text, one token, finish stop."""
    for choice in payload.get("choices") or []:
        choice.update(text="", finish_reason="stop", logprobs=None)
    usage = payload.get("usage")
    if isinstance(usage, dict):
        usage["completion_tokens"] = len(payload.get("choices") or [None])
        usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage["completion_tokens"]


def end_stream_at_once(stream: str) -> str:
    """The same for a Completions stream: one empty text frame finishing stop, then usage."""
    frames = [
        json.loads(line[len("data: ") :])
        for line in stream.split("\n\n")
        if line.startswith("data: {")
    ]
    texts = [frame for frame in frames if frame.get("choices")]
    kept = []
    if texts:
        last = texts[-1]
        for choice in last["choices"]:
            choice.update(text="", finish_reason="stop", logprobs=None)
        kept.append(last)
    for frame in frames:
        if not frame.get("choices") and isinstance(frame.get("usage"), dict):
            end_at_once(frame)
            kept.append(frame)
    return "".join(f"data: {json.dumps(f)}\n\n" for f in kept) + "data: [DONE]\n\n"


def account_input(payload: dict, size: int) -> None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return
    usage["prompt_tokens" if "prompt_tokens" in usage else "input_tokens"] = size
    output = usage.get("completion_tokens", usage.get("output_tokens"))
    if "total_tokens" in usage and isinstance(output, int):
        usage["total_tokens"] = size + output


def sort_answer_keys(payload: dict) -> None:
    """Rewrite a JSON answer with sorted keys, as a hop that sorted the schema would cause."""
    holders = [(c.get("message", {}), "content") for c in payload.get("choices") or []]
    holders += [(b, "text") for b in payload.get("content") or [] if isinstance(b, dict)]
    for item in payload.get("output") or []:
        holders += [(part, "text") for part in item.get("content") or []]
    for holder, key in holders:
        text = holder.get(key)
        start = text.find("{") if isinstance(text, str) else -1
        if start < 0:
            continue
        try:
            value = json.loads(text[start:])
        except ValueError:
            continue
        holder[key] = text[:start] + json.dumps(value, sort_keys=True)


# Fields no shared check reads, so only a contract baseline sees them change.
DRIFT_RENAMES = {
    "reasoning_content": "reasoning",
    "stop_sequence": "matched_stop",
    "incomplete_details": "incomplete",
}


def _renamed(value):
    if isinstance(value, dict):
        return {DRIFT_RENAMES.get(k, k): _renamed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_renamed(v) for v in value]
    return value


def _retyped_arguments(value):
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if key == "arguments" and isinstance(child, str):
                out[key] = json.loads(child)
            elif key == "input" and value.get("type") == "tool_use":
                out[key] = json.dumps(child)
            else:
                out[key] = _retyped_arguments(child)
        return out
    if isinstance(value, list):
        return [_retyped_arguments(child) for child in value]
    return value


def drift_payload(defect: str | None, code: int, payload: dict) -> dict:
    """`renamed` renames fields of a generation; `tool_args_retyped` sends tool arguments in the
    other JSON type, an object for a string and a string for an object, as a translating layer
    can; `error_renamed` replaces the error envelope with a framework default; `error_retyped`
    sends the error code as a string."""
    if defect == "renamed" and code == 200:
        return _renamed(payload)
    if defect == "tool_args_retyped" and code == 200:
        return _retyped_arguments(payload)
    if code >= 400 and isinstance(payload.get("error"), dict):
        if defect == "error_renamed":
            return {"detail": payload["error"].get("message")}
        if defect == "error_retyped":
            return {"error": {**payload["error"], "code": str(code)}}
    return payload


def drift_stream(defect: str | None, text: str) -> str:
    """`stream_renamed` moves usage to another key in every frame, as a release can."""
    return text.replace('"usage":', '"usage_info":') if defect == "stream_renamed" else text
