"""A spec-faithful Chat and Completions engine for the OpenAI contract cases.

`FakeFleet(openai_contract=True)` hands Chat and Completions requests here. The fake follows
the published shapes (openai SDK `chat/chat_completion.py`, `chat/chat_completion_chunk.py`,
`completion.py`) and honours token limits, stop strings, logprobs, `n`, prompt forms, echo,
sampling repeats and the engine options the contract cases exercise. Each `defect` is one way a
stack gets one of those wrong, and `field_status` answers a named request field with a fixed
status, as an ingress that refuses or crashes on it does. With `reasons`, Chat writes the count
it is asked for in its reasoning first unless the request turns reasoning off, and a stop string
matches there as in the answer, as vLLM does: a healthy engine, since stop with reasoning on has
no contract.

Tokens are whitespace-delimited pieces of the answer, so a count is easy to reason about.
"""

from __future__ import annotations

import itertools
import json
import random
import re
import time
import uuid
from typing import Any

ESSAY = "Rivers carve deep valleys across ancient stone and carry silt toward the sea".split()
WORDS = "amber birch cobalt dune ember fjord grove heron iris juniper kelp lagoon".split()
_REPLAYED: dict[str, tuple[int, dict | None, str | None]] = {}
# Successive temperature-0 completions under `greedy_unstable` never repeat.
_UNSTABLE = (f"draw{n}" for n in itertools.count())
# The proof prompt reasons this many pieces before it answers, in the `reasoning` field.
PROOF_THOUGHT = 200


def _text(content: Any, defect: str | None) -> str:
    if isinstance(content, list):
        parts = [
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ]
        return parts[0] if defect == "first_part_only" and parts else "".join(parts)
    return content if isinstance(content, str) else ""


def _messages(body: dict, defect: str | None) -> list[dict]:
    messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    if defect == "drops_developer":
        messages = [m for m in messages if m.get("role") != "developer"]
    return messages


def _prompt(body: dict, path: str, defect: str | None) -> str:
    if path == "/v1/completions":
        return ""
    return " ".join(_text(m.get("content"), defect) for m in _messages(body, defect))


def _number_text(body: dict, defect: str | None) -> str:
    """The rendered tool schemas, as an engine renders them into the prompt.

    `excludes_tools_on_none` is a deployment option, not a defect: vLLM's
    `--exclude-tools-when-tool-choice-none` renders a `tool_choice: none` request without them.
    """
    tools = body.get("tools")
    if tools is None or (defect == "excludes_tools_on_none" and body.get("tool_choice") == "none"):
        return ""
    if defect == "float_numbers":
        tools = json.loads(json.dumps(tools), parse_int=float)
    return json.dumps(tools)


def _rng(body: dict, defect: str | None) -> random.Random | None:
    """None for greedy decoding; a seeded generator when the request carries a seed."""
    greedy = body.get("temperature", 1) == 0 and defect != "greedy_unstable"
    if greedy:
        return None
    seed = body.get("seed")
    return random.Random(seed if seed is not None and defect != "seed_ignored" else None)


def _answer(body: dict, path: str, defect: str | None) -> list[str]:
    """The pieces a healthy model would produce, before limits and stop strings."""
    messages = _messages(body, defect)
    asked = _prompt(body, path, defect)
    if path == "/v1/completions":
        prompt = body.get("prompt")
        prompt = prompt if isinstance(prompt, str) else ""
        if "Count from 1 to 10" in prompt:
            last = max(int(n) for n in re.findall(r"\d+", prompt.split(":", 1)[-1]) or ["0"])
            return [f"{',' if i else ''} {n}" for i, n in enumerate(range(last + 1, 11))]
        if "long essay" in prompt:
            return [f" {ESSAY[i % len(ESSAY)]}" for i in range(400)]
        if defect == "greedy_unstable" and body.get("temperature") == 0:
            # An engine whose temperature-0 decode varies from one request to the next.
            return [" Paris", " is", " the", " capital", f" {next(_UNSTABLE)}", "."]
        return [" Paris", " is", " the", " capital", "."]
    if "Count from 1 to 10" in asked:
        return ["1", *(f", {n}" for n in range(2, 11))]
    if "long essay" in asked:
        return [f"{' ' if i else ''}{ESSAY[i % len(ESSAY)]}" for i in range(400)]
    if "random six-word sentence" in asked:
        rng = _rng(body, defect)
        words = WORDS[:6] if rng is None else [rng.choice(WORDS) for _ in range(6)]
        return [f"{' ' if i else ''}{w}" for i, w in enumerate(words)]
    if "exact maximum" in asked:
        tools = json.loads(_number_text(body, defect) or "[]")
        if not tools:
            return ["I", " see", " no", " tool."]
        bound = tools[0]["function"]["parameters"]["properties"]["count"]["maximum"]
        return [json.dumps(bound)]
    if "infinitely many primes" in asked:
        return ["Euclid", " proved", " it."]
    match = re.search(r"Repeat exactly, nothing else: (.*)$", asked, re.S)
    if match:
        said = match.group(1)
        if defect == "mojibake":
            said = "".join(c if ord(c) < 128 else "\ufffd" for c in said)
        return list(said)
    last = _text(messages[-1].get("content"), defect) if messages else ""
    if "Add 9 to that" in last:
        return ["400"]
    kwargs = body.get("chat_template_kwargs") or {}
    if kwargs.get("enable_thinking") is False and "17 times 23" in asked:
        thought = "17 times 23 is 340 plus 51, so "
        return [thought + "391391"] if defect == "thinking_leak" else ["391"]
    match = re.search(r"Say: (.*)$", asked, re.S)
    answer = match.group(1).strip() if match else ("391" if "17 times 23" in asked else "ok")
    instructions = " ".join(
        _text(m.get("content"), defect)
        for m in messages
        if m.get("role") in ("system", "developer")
    )
    if "uppercase" in instructions:
        answer = answer.upper()
    return [f"{' ' if i else ''}{w}" for i, w in enumerate(answer.split())]


EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


# Per-sample noise of +-40% around ordered means, a fixed cycle so the case is deterministic.
# Each effort walks the cycle from its own phase: one sample per effort sees low 30, medium 56
# and high 30, inverted, while any three consecutive samples of an effort hold the whole cycle,
# so its median is its mean.
EFFORT_NOISE = (1.4, 0.6, 1.0)
_EFFORT_SAMPLES: dict[int, int] = {}


def reset_effort_noise() -> None:
    _EFFORT_SAMPLES.clear()


def _effort_noise(rank: int) -> float:
    sample = _EFFORT_SAMPLES.get(rank, 0)
    _EFFORT_SAMPLES[rank] = sample + 1
    return EFFORT_NOISE[(rank + sample) % len(EFFORT_NOISE)]


def _thought(asked: str, body: dict, defect: str | None) -> list[str]:
    """Reasoning a prompt produces before its answer; most fake prompts answer directly."""
    if "strawberry raspberry" in asked:
        rank = EFFORTS.index(body.get("reasoning_effort") or "medium")
        if defect == "effort_ignored":
            rank = 3
        elif defect == "effort_inverted" and rank == 3:
            rank = 9
        length = 10 * (rank + 1)
        if defect == "effort_noisy":
            length = round(length * _effort_noise(rank))
        return [f" {ESSAY[i % len(ESSAY)]}" for i in range(length)]
    if "infinitely many primes" in asked:
        return [f" {ESSAY[i % len(ESSAY)]}" for i in range(PROOF_THOUGHT)]
    if "17 times 23" in asked and "include_reasoning" in body:
        words = "17 times 20 is 340, 17 times 3 is 51, and 340 plus 51 is 391.".split()
        return [f"{' ' if i else ''}{w}" for i, w in enumerate(words)]
    return []


def _after_thought(
    body: dict, defect: str | None, thought: list[str], pieces: list[str], finish: str
) -> tuple[list[str], list[str], str]:
    """Charge the thought to the token limit before the answer."""
    limit = _limit(body, defect)
    length = "stop" if defect == "length_as_stop" else "length"
    if limit is not None and len(thought) >= limit:
        return thought[:limit], [], length
    if limit is not None and len(thought) + len(pieces) > limit:
        return thought, pieces[: limit - len(thought)], length
    return thought, pieces, finish


def _structured(body: dict, defect: str | None) -> list[str] | None:
    fmt = body.get("response_format") or {}
    if fmt.get("type") == "json_schema" and defect != "response_format_ignored":
        props = ((fmt.get("json_schema") or {}).get("schema") or {}).get("properties") or {}
        keys = sorted(props) if defect == "sorted_keys" else list(props)
        value = {k: (391 if props[k].get("type") == "integer" else "short") for k in keys}
        return [json.dumps(value)]
    choice = (body.get("structured_outputs") or {}).get("choice") or body.get("guided_choice")
    if choice and defect != "structured_outputs_ignored":
        return [choice[0]]
    if choice:
        return ["something", " else"]
    return None


def _limit(body: dict, defect: str | None) -> int | None:
    if defect == "limit_ignored":
        return None
    if defect == "max_tokens_wins" and body.get("max_tokens") is not None:
        return body["max_tokens"]
    return body.get("max_completion_tokens", body.get("max_tokens"))


def _generate(body: dict, path: str, defect: str | None) -> tuple[list[str], str, str | None]:
    """Apply min_tokens, ignore_eos, the token limit and stop strings, as an engine does.

    The third value is the stop string that ended the text, which vLLM reports as `stop_reason`.
    """
    if defect == "seed_empties_reply" and "seed" in body:
        # A layer that answers a request carrying a seed with 200 and no text.
        return [""], "stop", None
    pieces = _structured(body, defect) or _answer(body, path, defect)
    limit = _limit(body, defect)
    forced = [k for k, v in (body.get("logit_bias") or {}).items() if v >= 100]
    if forced and limit and defect != "logit_bias_ignored":
        # A +100 bias selects its token every step, so the reply runs to the limit.
        pieces = [f" t{forced[0]}"] * (limit + 1)
    banned = body.get("bad_words") or []
    if banned and defect != "bad_words_ignored":
        pieces = [p for p in pieces if p.strip() not in banned] or ["ok"]
    if body.get("allowed_token_ids") and limit:
        # Allowed ids rarely include end-of-sequence, so the reply runs to the limit.
        pieces = [f" {WORDS[i % len(WORDS)]}" for i in range(limit + 1)]
    if body.get("ignore_eos") and limit:
        pieces = (pieces + [f" {w}" for w in ESSAY] * limit)[:limit]
    floor = body.get("min_tokens") or 0
    if len(pieces) < floor:
        pieces = pieces + [f" {ESSAY[i % len(ESSAY)]}" for i in range(floor - len(pieces))]
    finish = "stop"
    if (
        limit is not None
        and len(pieces) >= limit
        and (body.get("ignore_eos") or len(pieces) > limit)
    ):
        pieces, finish = pieces[:limit], ("stop" if defect == "length_as_stop" else "length")
    stops = body.get("stop")
    stops = [stops] if isinstance(stops, str) else list(stops or [])
    if stops and defect != "stop_ignored":
        keep = body.get("include_stop_str_in_output") or defect == "stop_included"
        text = ""
        for count, piece in enumerate(pieces, 1):
            text += piece
            hits = [(text.find(s), s) for s in stops if s in text]
            if hits:
                at, matched = min(hits)
                cut = text[: at + len(matched)] if keep else text[:at]
                consumed = "".join(pieces[: count - 1])
                pieces = pieces[: count - 1] + [cut[len(consumed) :]]
                pieces = [p for p in pieces if p] or [""]
                return pieces, "stop", None if defect == "stop_reason_missing" else matched
    return pieces, finish, None


def _token_logprobs(
    pieces: list[str],
    width: int,
    defect: str | None,
    as_ids: bool = False,
    allowed: list[int] | None = None,
) -> list[dict]:
    entries = []
    for number, piece in enumerate(pieces):
        token_id = allowed[number % len(allowed)] if allowed else 1000 + number
        if allowed and defect == "allowed_tokens_ignored":
            token_id = 2000 + number
        if as_ids and defect != "token_strings":
            piece = f"token_id:{token_id}"
        alts = [{"token": piece, "logprob": -0.25, "bytes": list(piece.encode())}]
        alts += [
            {"token": w, "logprob": -2.0 - i, "bytes": list(w.encode())}
            for i, w in enumerate(WORDS[: width + 1])
        ]
        top = alts[: width + 1] if defect == "top_width" else alts[:width]
        entries.append(
            {"token": piece, "logprob": -0.25, "bytes": list(piece.encode()), "top_logprobs": top}
        )
    return entries[:-1] if defect == "logprobs_short" and entries else entries


def _usage(prompt: int, completion: int, defect: str | None) -> dict:
    cached = prompt + 5 if defect == "cached_over_prompt" else 0
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
    }
    if defect == "prediction_over_completion":
        usage["completion_tokens_details"] = {"rejected_prediction_tokens": completion + 3}
    return usage


def _envelope(status: int, field: str) -> dict:
    kind = "invalid_request_error" if status < 500 else "server_error"
    return {"error": {"message": f"{field} is not supported", "type": kind, "param": field}}


def reply(
    path: str,
    body: dict,
    model: str,
    defect: str | None,
    field_status: dict | None,
    reasons: bool = False,
) -> tuple[int, dict | None, str | None]:
    """(status, JSON payload, SSE text) for one Chat or Completions request."""
    for name, status in (field_status or {}).items():
        if name in body or name in (body.get("stream_options") or {}):
            return status, _envelope(status, name), None
    if defect == "null_crash" and any(value is None for value in body.values()):
        return 500, _envelope(500, "null"), None
    history = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    if defect == "rejects_reasoning_history" and any(
        m.get("role") == "assistant" and ("reasoning" in m or "reasoning_content" in m)
        for m in history
    ):
        return 400, _envelope(400, "messages"), None
    if not body.get("stream") and body.get("stream_options") is not None:
        return 400, _envelope(400, "stream_options"), None
    key = json.dumps(body, sort_keys=True)
    if defect == "replay" and key in _REPLAYED:
        return _REPLAYED[key]
    if path == "/v1/completions":
        result = _completions(body, model, defect)
    else:
        result = _chat(body, model, defect, reasons)
    if defect == "replay":
        _REPLAYED[key] = result
    return result


# ------------------------------------------------------------------ chat


def _reasoning_off(body: dict) -> bool:
    kwargs = body.get("chat_template_kwargs") or {}
    return body.get("reasoning_effort") == "none" or kwargs.get("enable_thinking") is False


def _stop_in_thought(
    body: dict, thought: list[str], pieces: list[str], finish: str, matched: str | None
) -> tuple[list[str], list[str], str, str | None]:
    """A stop string met in the reasoning ends the request there, before any answer."""
    stops = body.get("stop")
    stops = [stops] if isinstance(stops, str) else list(stops or [])
    text = "".join(thought)
    hits = sorted((text.find(s), s) for s in stops if s in text)
    if not hits:
        return thought, pieces, finish, matched
    at, stop = hits[0]
    cut = text[: at + len(stop)] if body.get("include_stop_str_in_output") else text[:at]
    return [cut], [], "stop", stop


def _chat(
    body: dict, model: str, defect: str | None, reasons: bool = False
) -> tuple[int, dict | None, str | None]:
    prompt_tokens = max(1, len(_prompt(body, "", defect).split())) + len(_number_text(body, defect))
    n = body.get("n") or 1
    pieces, finish, matched = _generate(body, "/v1/chat/completions", defect)
    created = int(time.time())
    ident = f"chatcmpl-{uuid.uuid4().hex}"
    continuation = None
    messages = body.get("messages") or []
    if body.get("continue_final_message") and messages and messages[-1].get("role") == "assistant":
        prefix = _text(messages[-1].get("content"), defect)
        tail = "".join(_answer({"messages": messages[:-1]}, "", defect))
        continuation = tail[len(prefix) :] if tail.startswith(prefix.rstrip(",")) else tail
        pieces = [continuation]
    asked = _prompt(body, "", defect)
    thought = _thought(asked, body, defect)
    if reasons and "Count from 1 to 10" in asked and not _reasoning_off(body):
        thought = ["The user wants", " 1, 2, 3, 4, 5, 6, 7, 8, 9, 10", ". Write it."]
        thought, pieces, finish, matched = _stop_in_thought(body, thought, pieces, finish, matched)
    if thought:
        thought, pieces, finish = _after_thought(body, defect, thought, pieces, finish)
    if defect == "cut_reasoning_in_content":
        thought, pieces = [], thought + pieces
    hidden = 0
    if body.get("include_reasoning") is False and defect != "include_reasoning_ignored":
        if defect == "hidden_reasoning_in_content":
            pieces = thought + pieces
        hidden, thought = len(thought), []
    if body.get("stream"):
        return (
            200,
            None,
            _chat_stream(
                body, model, defect, pieces, finish, ident, created, prompt_tokens, thought, hidden
            ),
        )
    text = "".join(pieces)
    message: dict[str, Any] = {
        "role": "assistant",
        "content": text if text or not thought else None,
        "refusal": None,
        "annotations": None,
        "tool_calls": [],
    }
    if continuation is not None:
        prefix = _text(messages[-1].get("content"), defect)
        echoed = prefix if body.get("echo") else ""
        if defect == "continuation_in_reasoning":
            message.update(content=echoed or None, reasoning=continuation)
        else:
            message["content"] = echoed + continuation
    if thought:
        message["reasoning"] = "".join(thought)
    if defect == "audio_unrequested":
        message["audio"] = {"id": "audio_1", "data": "", "expires_at": 0, "transcript": ""}
    if defect == "tool_calls_without_tools":
        message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": "x"}}]
    logprobs = None
    if body.get("logprobs"):
        width, as_ids = body.get("top_logprobs") or 0, bool(body.get("return_tokens_as_token_ids"))
        allowed = body.get("allowed_token_ids")
        logprobs = {"content": _token_logprobs(pieces, width, defect, as_ids, allowed)}
    elif defect == "logprobs_unrequested":
        logprobs = {"content": []}
    choices = [
        {
            "index": i,
            "message": dict(message),
            "logprobs": logprobs,
            "finish_reason": "eos" if defect == "finish_unknown" else finish,
            "stop_reason": matched if finish == "stop" else None,
        }
        for i in range(n)
    ]
    payload = {
        "id": "" if defect == "empty_id" else ident,
        "object": "chat.completion",
        "created": created * 1000 if defect == "created_ms" else created,
        "model": model,
        "system_fingerprint": "fp_fake",
        "service_tier": None,
        "choices": choices,
        "usage": _usage(prompt_tokens, (hidden + len(thought) + len(pieces)) * n, defect),
    }
    if defect == "moderation_unrequested":
        payload["moderation"] = {"input": {"type": "error", "code": "x", "message": "x"}}
    if body.get("prompt_logprobs") is not None:
        width = body["prompt_logprobs"]
        entries = [None] + [
            {str(1000 + i): {"logprob": -1.5, "rank": 1, "decoded_token": "w"}}
            for i in range(prompt_tokens - 1)
        ]
        if defect == "prompt_logprobs_short":
            entries = entries[:-1]
        payload["prompt_logprobs"] = entries if width >= 0 else None
    return 200, payload, None


def _chat_stream(
    body, model, defect, pieces, finish, ident, created, prompt_tokens, thought=(), hidden=0
) -> str:
    options = body.get("stream_options") or {}
    continuous = options.get("continuous_usage_stats") and defect != "continuous_usage_ignored"
    width = body.get("top_logprobs") or 0
    frames: list[dict] = []
    emitted = 0

    def frame(delta: dict, finish_reason=None, logprobs=None, count=0) -> None:
        nonlocal emitted
        emitted += count
        out = {
            "id": f"chatcmpl-{uuid.uuid4().hex}" if defect == "chunk_id_changes" else ident,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {"index": 0, "delta": delta, "logprobs": logprobs, "finish_reason": finish_reason}
            ],
        }
        if options.get("include_obfuscation"):
            out["obfuscation"] = "x" * 4
        if continuous:
            reported = emitted if defect != "continuous_usage_decreasing" else max(0, 3 - emitted)
            out["usage"] = _usage(prompt_tokens, reported, None)
        frames.append(out)

    if defect == "stream_text_differs":
        pieces = [*pieces, " extra"]
    frame({} if defect == "no_role" else {"role": "assistant", "content": ""})
    for piece in thought:
        frame({"reasoning": piece}, count=1)
    for piece in pieces:
        logprobs = None
        if body.get("logprobs"):
            logprobs = {"content": _token_logprobs([piece], width, None)}
        frame({"content": piece}, logprobs=logprobs, count=1)
    if defect == "reasoning_after_content":
        frame({"reasoning": "late thought"})
    if body.get("logprobs") and defect == "logprobs_short":
        frames[-1]["choices"][0]["logprobs"] = None
    frame({}, finish_reason=finish)
    if defect == "chunk_after_finish":
        frame({"content": " more"})
    if options.get("include_usage") or (
        defect == "usage_when_false" and "include_usage" in options
    ):
        usage = {
            "id": ident,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}}] if defect == "usage_chunk_choice" else [],
            "usage": _usage(
                prompt_tokens + (7 if defect == "stream_prompt_count_differs" else 0),
                hidden + len(thought) + len(pieces),
                None,
            ),
        }
        frames.append(usage)
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"


# ------------------------------------------------------------------ completions


def _prompts(body: dict, defect: str | None) -> list[tuple[str, int]]:
    """Each prompt with its token count: a string, a list of them, token ids or lists of ids."""
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        items = [prompt]
    elif isinstance(prompt, list) and prompt and all(isinstance(p, int) for p in prompt):
        items = [prompt]
    else:
        items = list(prompt or [])
    if defect == "prompt_list_first_only":
        items = items[:1]
    out = []
    for item in items:
        if isinstance(item, str):
            out.append((item, max(1, len(item.split()))))
        else:
            count = len(str(item)) if defect == "token_ids_as_text" else len(item)
            out.append((" ".join(f"t{i}" for i in item), count))
    cap = body.get("truncate_prompt_tokens")
    if isinstance(cap, int) and cap > 0 and defect != "truncation_ignored":
        out = [(text, min(count, cap)) for text, count in out]
    return out


def _completions(body: dict, model: str, defect: str | None) -> tuple[int, dict | None, str | None]:
    prompts = _prompts(body, defect)
    n = body.get("n") or 1
    pieces, finish, matched = _generate(body, "/v1/completions", defect)
    created = int(time.time())
    ident = f"cmpl-{uuid.uuid4().hex}"
    usage = _usage(sum(c for _, c in prompts), len(pieces) * n * len(prompts), defect)
    if body.get("stream"):
        return (
            200,
            None,
            _completion_stream(body, model, defect, pieces, finish, ident, created, usage),
        )
    choices = []
    for p, (text, count) in enumerate(prompts):
        for j in range(n):
            echoed = text if body.get("echo") and defect != "echo_ignored" else ""
            choice = {
                "index": p * n + j,
                "text": echoed + "".join(pieces),
                "logprobs": None,
                "finish_reason": "eos" if defect == "finish_unknown" else finish,
                "stop_reason": matched,
            }
            if body.get("return_token_ids"):
                output = len(pieces) - (defect == "token_ids_short")
                choice["prompt_token_ids"] = [1000 + i for i in range(count)]
                choice["token_ids"] = [2000 + i for i in range(output)]
            if body.get("logprobs") is not None:
                choice["logprobs"] = _text_logprobs(pieces, body["logprobs"], defect)
                if echoed and defect != "echo_logprobs_short":
                    choice["logprobs"] = _echo_logprobs(
                        echoed, choice["logprobs"], body["logprobs"]
                    )
            choices.append(choice)
    payload = {
        "id": "" if defect == "empty_id" else ident,
        "object": "text_completion",
        "created": created * 1000 if defect == "created_ms" else created,
        "model": model,
        "system_fingerprint": "fp_fake",
        "choices": choices,
        "usage": usage,
    }
    return 200, payload, None


def _text_logprobs(pieces: list[str], width: int, defect: str | None) -> dict:
    offsets, at = [], 0
    for piece in pieces:
        offsets.append(at)
        at += len(piece)
    tops = [
        {piece: -0.25, **{w: -2.0 - i for i, w in enumerate(WORDS[:width])}} for piece in pieces
    ]
    tops = [dict(list(top.items())[:width]) if width else top for top in tops]
    result = {
        "tokens": list(pieces),
        "token_logprobs": [-0.25] * len(pieces),
        "top_logprobs": tops,
        "text_offset": offsets,
    }
    if defect == "text_logprobs_misaligned":
        result["text_offset"] = offsets[:-1]
    elif defect == "text_logprobs_non_string_token":
        result["tokens"][0] = 1000
    elif defect == "text_logprobs_string_score":
        result["top_logprobs"][0][pieces[0]] = "invalid"
    elif defect == "text_logprobs_positive_score":
        result["top_logprobs"][0][pieces[0]] = 0.25
    elif defect == "text_logprobs_infinite_score":
        result["top_logprobs"][0][pieces[0]] = float("inf")
    return result


def _echo_logprobs(prompt: str, completion: dict, width: int) -> dict:
    """Prompt entries ahead of the completion's: the first prompt token has no logprob."""
    words = prompt.split()
    tokens = [f"{' ' if i else ''}{w}" for i, w in enumerate(words)]
    scored = _text_logprobs(tokens, width, None)
    shift = len(prompt)
    return {
        "tokens": tokens + completion["tokens"],
        "token_logprobs": [None] + scored["token_logprobs"][1:] + completion["token_logprobs"],
        "top_logprobs": [None] + scored["top_logprobs"][1:] + completion["top_logprobs"],
        "text_offset": scored["text_offset"] + [shift + o for o in completion["text_offset"]],
    }


def _completion_stream(body, model, defect, pieces, finish, ident, created, usage) -> str:
    frames = []
    if defect == "stream_text_differs":
        pieces = [*pieces, " extra"]
    for i, piece in enumerate(pieces):
        last = i == len(pieces) - 1
        frame = {
            "id": ident,
            "object": "text_completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "text": piece,
                    "logprobs": None,
                    "finish_reason": finish if last else None,
                }
            ],
        }
        if defect == "completion_chunk_index_missing":
            del frame["choices"][0]["index"]
        elif defect == "completion_chunk_index_bool":
            frame["choices"][0]["index"] = False
        elif defect == "completion_chunk_index_wrong":
            frame["choices"][0]["index"] = 99
        frames.append(frame)
    if (body.get("stream_options") or {}).get("include_usage"):
        streamed = dict(usage, completion_tokens=len(pieces))
        if defect == "stream_prompt_count_differs":
            streamed["prompt_tokens"] += 7
        streamed["total_tokens"] = streamed["prompt_tokens"] + len(pieces)
        frames.append(
            {
                "id": ident,
                "object": "text_completion",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "text": "", "finish_reason": None}]
                if defect == "usage_chunk_choice"
                else [],
                "usage": streamed,
            }
        )
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
