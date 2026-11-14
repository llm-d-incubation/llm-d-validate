"""HTTP and SSE behavior of the fake fleet, with the transport defects the suite must catch.

`FakeFleet(transport={...})` turns these on. Keys:

- `defects`: a set of defect names, each a real way a layer mishandles HTTP (see DEFECTS).
- `stream_gap_s`, `first_event_delay_s`: pacing of chat and completions streams.
- `chat_stream`: raw bytes served for a chat stream, for framing defects.
- `reasoning_field`: emit chat stream reasoning in this delta field. The default is inline tags.
- `cors`: "allowed" answers a CORS preflight; anything else refuses it.
- `credential_headers`: alternate headers accepted for the fleet's `auth_header` key.
- `idempotency`: "replayed" returns the stored response for a repeated Idempotency-Key.
- `get_paths`: {path: status} answered to GET, for exposure declarations.
- `alpn`: ALPN protocols the TLS listener offers.
- `slow_first_s`: the first generation request sleeps this long, so a client can time out.
- `retry_generation`: a chat stream with x-stainless-retry-count above zero is held
  `held_s` (default 0) before its headers, then spreads its frames over `tokens / tokens_per_s`
  and reports `tokens` completion tokens, as a long generation served at that rate would be.
  `usage_choice` sends its usage frame with the finished choice in it, as LiteLLM does (#213).
- `multimodal`: accept image input.
- `model_rewrite`: a header naming the served model for a request that names an alias.
- `max_body_bytes`: a request body above this size answers 413.

Without a `transport` mapping the fake makes the corrections every real server makes: HEAD and
OPTIONS answer 405, a query string does not change the route, a chunked or gzip request body is
read, and a request variant a server refuses gets a 400: empty input, an unknown role, invalid
image data, an image to a text-only model, and an unknown Messages thinking type.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import re
import sys
import time
import uuid
from http.server import ThreadingHTTPServer

# A client that hangs up before the reply is written: a test that disconnects on purpose, or a
# client timeout under a parallel run. Not an error in the fake.
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class QuietServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that stays silent when the client has already gone.

    The stock server prints a traceback from its request thread for any error, including
    writing to a socket the client closed. That traceback lands in the test log and looks like
    a crash although no test failed. Every other error is still printed.
    """

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], _CLIENT_GONE):
            return
        super().handle_error(request, client_address)


DEFECTS = frozenset(
    {
        "method_5xx",  # HEAD and OPTIONS reach a handler that does not exist: 501
        "no_allow_header",  # 405 without Allow
        "charset_415",  # a charset parameter on Content-Type is refused
        "content_type_500",  # missing or text/plain Content-Type crashes the parser
        "accept_sse",  # Accept: text/event-stream turns a non-streaming request into SSE
        "gzip_500",  # a gzip body is parsed as JSON bytes and crashes
        "gzip_always",  # responses are gzip whatever Accept-Encoding offered
        "buffer_compressed_stream",  # a stream offered gzip is buffered and sent at once
        "chunked_411",  # a chunked body is refused with Length Required
        "expect_417",  # Expect: 100-continue is refused
        "close_connections",  # every response closes its connection
        "malformed_body_500",  # an unparseable body crashes the server
        "trailing_bytes_served",  # bytes after the JSON value are ignored
        "long_header_431",  # a 4 KiB header value is refused
        "header_case_sensitive",  # an upper-case anthropic-version is not recognized
        "underscore_502",  # a header name with an underscore gets 502 from a proxy that rejects it
        "client_header_400",  # an SDK retry header with a count above zero is refused
        "query_string_404",  # a query string changes the route
        "path_variant_500",  # trailing or double slashes and encoded slashes crash routing
        "auth_500",  # a malformed Authorization header crashes the auth layer
        "rejects_authorization",  # an unauthenticated layer refuses a client's Authorization
        "should_retry_true",  # 4xx responses carry x-should-retry: true
        "client_error_503",  # a deterministic client error answers 503
        "anthropic_error_type",  # Messages errors carry a non-Anthropic error type
        "openai_error_untyped",  # OpenAI errors carry no type
        "bodiless_header_honored",  # a routing header on GET /v1/models reaches a closed port
        "wedge_after_disconnect",  # after a client disconnect every request answers 503
        "slow_after_disconnect",  # after a client disconnect the next request waits
        "stall",  # one long gap in the middle of a stream
        "retry_rejected",  # a request with x-stainless-retry-count above zero answers 409
        "idempotency_replay",  # a repeated Idempotency-Key returns the first response
        "accepts_empty_input",
        "accepts_unknown_role",
        "variant_500",  # every request-variant error answers 500
        "image_inlined",  # an image is accepted by a text-only model
        "thinking_type_ignored",
        "rate_limit_garbled",  # rate-limit headers carry values no client can parse
        "sse_unknown_field",  # a stream line with a field name SSE does not define
        "sse_invalid_utf8",  # a frame carries a byte that is not UTF-8
        "sse_partial_line",  # the stream ends without a final line terminator
        "after_done_frame",  # a data frame follows data: [DONE]
        "role_late",  # the role arrives after the first choice frame
        "reasoning_after_content",  # a reasoning delta arrives after the answer started
        "completions_two_finishes",  # a completions stream finishes twice
        "completions_error_frame",  # a completions stream reports an error as a data frame
        "completions_empty_choice_frame",  # a choice-less frame has no usage payload
        "completions_chat_object",  # completions stream frames carry the chat object type
        "rewrite_echoes_served",  # a rewritten request echoes the served model name
        "body_limit_500",  # a body above the limit crashes the server
        "sentinel_ends_stream",  # a frame whose text contains [DONE] becomes a finish (LiteLLM)
    }
)

STREAM_DEFECTS = frozenset(
    {
        "stall",
        "buffer_compressed_stream",
        "sse_unknown_field",
        "sse_invalid_utf8",
        "sse_partial_line",
        "after_done_frame",
        "role_late",
        "reasoning_after_content",
        "sentinel_ends_stream",
    }
)

STREAM_ANSWER = ["<think>", "weighing", " it up", "</think>", "o", "k"]
ANTHROPIC_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
}


# A prompt that asks for one line verbatim; a healthy fake model writes the line.
_REPEAT = re.compile(r'write nothing else: ([^"\\]+)')


def requested_line(body: dict) -> str | None:
    """The line a prompt asks the model to repeat, as `streaming.sentinel_in_content` does."""
    asked = json.dumps([body.get(key) for key in ("messages", "input", "prompt")])
    found = _REPEAT.search(asked)
    return found.group(1) if found else None


def config(fleet) -> dict:
    return getattr(fleet, "transport", None) or {}


def defects(fleet) -> frozenset:
    found = frozenset(config(fleet).get("defects") or ())
    unknown = found - DEFECTS
    if unknown:
        raise ValueError(f"unknown transport defects {sorted(unknown)}")
    return found


def state(fleet) -> dict:
    if not hasattr(fleet, "_transport_state"):
        fleet._transport_state = {"disconnected": False, "posts": 0, "replays": {}}
    return fleet._transport_state


def _error(handler, code: int, message: str, extra_headers: dict | None = None) -> None:
    handler._send(
        code,
        {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": None,
            }
        },
        extra_headers=extra_headers,
    )


def _plain(handler, code: int, text: str, headers: dict | None = None) -> None:
    body = text.encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html")
    handler.send_header("Content-Length", str(len(body)))
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


# ------------------------------------------------------------------ request side


def _read_chunked(handler) -> bytes:
    data = b""
    while True:
        size = int(handler.rfile.readline().strip().split(b";")[0], 16)
        if size == 0:
            while handler.rfile.readline().strip():
                pass
            return data
        data += handler.rfile.read(size)
        handler.rfile.readline()


def _path_and_query(handler) -> tuple[str, str]:
    path, _, query = handler.path.partition("?")
    return path, query


def post(fleet, handler) -> tuple[bool, bytes | None]:
    """Run before the fake's own POST handling: (answered, the request body or None if answered).

    The body is always read here first, so a connection stays usable after an early answer and
    the fake's own handler never reads it. Recorded headers keep the wire Content-Length.
    """
    found = defects(fleet)
    current = state(fleet)
    headers = handler.headers
    path, query = _path_and_query(handler)
    if query and not getattr(fleet, "refuse_query_string", False):
        handler.path = path
    chunked = "chunked" in (headers.get("Transfer-Encoding") or "").lower()
    if chunked:
        raw = _read_chunked(handler)
    else:
        length = int(headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
    if query and "query_string_404" in found:
        _error(handler, 404, "Not Found")
        return True, None
    limit = config(fleet).get("max_body_bytes")
    if limit and len(raw) > limit:
        if "body_limit_500" in found:
            _plain(handler, 500, "upstream reset")
        else:
            _error(handler, 413, "request body too large")
        return True, None
    if current["disconnected"] and "wedge_after_disconnect" in found:
        _error(handler, 503, "engine wedged")
        return True, None
    if current["disconnected"] and "slow_after_disconnect" in found:
        current["disconnected"] = False
        time.sleep(1.5)
    if chunked and "chunked_411" in found:
        _error(handler, 411, "Length Required")
        return True, None
    names = list(headers.keys())
    if "underscore_502" in found and any("_" in name for name in names):
        _plain(handler, 502, "<html><body><h1>502 Bad Gateway</h1></body></html>")
        return True, None
    if "long_header_431" in found and any(len(value) > 2048 for value in headers.values()):
        _error(handler, 431, "Request Header Fields Too Large")
        return True, None
    if "header_case_sensitive" in found and "ANTHROPIC-VERSION" in names:
        _error(handler, 400, "missing API version")
        return True, None
    retried = headers.get("x-stainless-retry-count", "0") != "0"
    if "client_header_400" in found and retried:
        _error(handler, 400, "unexpected retry header")
        return True, None
    if "retry_rejected" in found and retried:
        _error(handler, 409, "duplicate request")
        return True, None
    if "path_variant_500" in found and (
        "//" in path or "%2f" in path.lower() or (path.endswith("/") and len(path) > 1)
    ):
        _error(handler, 500, "routing failure")
        return True, None
    content_type = headers.get("Content-Type")
    if "charset_415" in found and content_type and ";" in content_type:
        _error(handler, 415, "Unsupported Media Type")
        return True, None
    if "content_type_500" in found and (
        content_type is None or content_type.startswith("text/plain")
    ):
        _error(handler, 500, "internal error")
        return True, None
    authorization = headers.get("Authorization")
    if "auth_500" in found and authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme != "Bearer" or not token.strip():
            _error(handler, 500, "auth crashed")
            return True, None
    expected = getattr(fleet, "auth_header", None)
    for alternate in config(fleet).get("credential_headers") or ():
        value = headers.get(alternate)
        if expected and authorization is None and value and expected[1].endswith(value):
            del handler.headers[expected[0]]
            handler.headers[expected[0]] = expected[1]
    if "rejects_authorization" in found and authorization:
        handler._send(401, {"error": {"message": "unexpected credential", "type": "auth"}})
        return True, None
    if (headers.get("Content-Encoding") or "").lower() == "gzip":
        if "gzip_500" in found:
            _error(handler, 500, "invalid JSON")
            return True, None
        try:
            raw = gzip.decompress(raw)
        except OSError:
            _error(handler, 400, "invalid gzip body")
            return True, None
    if _body_edges(handler, raw, found):
        return True, None
    if "trailing_bytes_served" in found:
        try:
            json.loads(raw)
        except ValueError:
            try:
                value, _ = json.JSONDecoder().raw_decode(raw.decode())
                raw = json.dumps(value).encode()
            except ValueError:
                pass
    if "accept_sse" in found and "text/event-stream" in (headers.get("Accept") or ""):
        try:
            value = json.loads(raw)
            value["stream"] = True
            raw = json.dumps(value).encode()
        except (ValueError, TypeError):
            pass
    raw = _rewrite_model(fleet, handler, raw)
    slow = config(fleet).get("slow_first_s")
    current["posts"] += 1
    if slow and current["posts"] == 1:
        time.sleep(slow)
    return False, raw


def _rewrite_model(fleet, handler, raw: bytes) -> bytes:
    """Serve an aliased model when the configured rewrite header names the served one."""
    # One handler serves every request on a keep-alive connection.
    handler.llmdv_alias = None
    header = config(fleet).get("model_rewrite")
    target = handler.headers.get(header) if header else None
    if not target:
        return raw
    try:
        value = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(value, dict) or "model" not in value:
        return raw
    handler.llmdv_alias = value["model"]
    value["model"] = target
    return json.dumps(value).encode()


def _body_edges(handler, raw: bytes, found) -> bool:
    """Answer a body the fake's own parser cannot: deep nesting, and the 5xx defect."""
    deep = b"[" * 1000 in raw
    if deep and "malformed_body_500" not in found:
        _error(handler, 400, "nesting too deep")
        return True
    if "malformed_body_500" in found:
        try:
            if not raw or deep:
                raise ValueError("empty or deep")
            json.loads(raw, object_pairs_hook=_no_duplicates)
        except (ValueError, RecursionError):
            _error(handler, 500, "parser crashed")
            return True
    return False


def _no_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def expect_100(fleet, handler) -> bool:
    if "expect_417" in defects(fleet):
        handler.send_response_only(417)
        handler.send_header("Content-Length", "0")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        return False
    handler.send_response_only(100)
    handler.end_headers()
    return True


def validate(fleet, handler, path: str, body) -> tuple[int, dict] | None:
    """Request-variant validation a real server performs and the base fake does not."""
    found = defects(fleet)
    if path not in ("/v1/chat/completions", "/v1/completions", "/v1/messages", "/v1/responses"):
        return None
    if not isinstance(body, dict):
        return None
    fail = 500 if "variant_500" in found else 400

    def bad(message, param=None):
        return fail, {
            "error": {"message": message, "type": "invalid_request_error", "param": param}
        }

    if "client_error_503" in found and body.get("model") not in (None, fleet.model):
        return 503, {"error": {"message": "no healthy upstream", "type": "service_unavailable"}}
    if body.get("model") is None and "variant_500" in found:
        return bad("model is required", "model")
    messages = body.get("messages")
    items = body.get("input")
    if (messages == [] or items == []) and "accepts_empty_input" not in found:
        return bad("at least one message is required", "messages")
    roles = {"system", "developer", "user", "assistant", "tool", "function"}
    for message in messages or (items if isinstance(items, list) else []) or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role is not None and role not in roles and "accepts_unknown_role" not in found:
            return bad(f"unknown role {role!r}", "messages")
        for block in _blocks(message.get("content")):
            image = _image_data(block)
            if image is None:
                continue
            try:
                base64.b64decode(image, validate=True)
            except (binascii.Error, ValueError):
                return bad("invalid base64 image data", "messages")
            if (
                not config(fleet).get("multimodal")
                and not (path == "/v1/messages" and fleet.anthropic is not None)
                and "image_inlined" not in found
            ):
                return bad(f"{fleet.model} is not a multimodal model", "messages")
    thinking = body.get("thinking")
    if (
        path == "/v1/messages"
        and fleet.anthropic is None  # the native backend validates its own thinking types
        and isinstance(thinking, dict)
        and thinking.get("type") not in ("enabled", "disabled", "adaptive")
        and "thinking_type_ignored" not in found
    ):
        return bad("thinking.type must be enabled, disabled or adaptive", "thinking")
    if "variant_500" in found:
        limit = body.get("max_tokens", body.get("max_output_tokens"))
        if path == "/v1/messages" and "max_tokens" not in body:
            return bad("max_tokens is required", "max_tokens")
        if isinstance(limit, int) and limit > fleet.max_model_len:
            return bad("max_tokens exceeds the context window", "max_tokens")
        if "tool_choice" in body and not body.get("tools"):
            return bad("tool_choice requires tools", "tool_choice")
        if "min_p" in body or "logit_bias" in body:
            return bad("sampling option not supported", "min_p")
        if any(isinstance(t, dict) and "input_schema" not in t for t in body.get("tools") or []):
            return bad("server tool", "tools")
    return None


def _blocks(content):
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if isinstance(block, dict):
            out.append(block)
            out.extend(_blocks(block.get("content")))
    return out


def _image_data(block: dict) -> str | None:
    if block.get("type") == "image":
        return (block.get("source") or {}).get("data")
    if block.get("type") == "image_url":
        url = (block.get("image_url") or {}).get("url") or ""
        return url.partition("base64,")[2] or None
    if block.get("type") == "input_image":
        url = block.get("image_url") or ""
        return url.partition("base64,")[2] or None
    return None


def idempotent(fleet, handler, path: str) -> dict | None:
    """A stored payload to replay for a repeated Idempotency-Key, where declared replayed."""
    key = handler.headers.get("Idempotency-Key")
    if not key or not _replays(fleet):
        return None
    return state(fleet)["replays"].get((path, key))


# ------------------------------------------------------------------ GET, HEAD, OPTIONS


def get(fleet, handler) -> bool:
    """Answer a GET the base fake does not know. True when answered."""
    found = defects(fleet)
    path, query = _path_and_query(handler)
    if query:
        handler.path = path
    paths = config(fleet).get("get_paths") or {}
    if path in paths:
        code = paths[path]
        payload = {"version": "0.0.0-fake"} if path == "/version" else {"status": code}
        handler._send(code, payload if code < 400 else {"detail": "Not Found"})
        return True
    if "bodiless_header_honored" in found and handler.headers.get("x-llmdv-destination"):
        _plain(handler, 502, "upstream connect error")
        return True
    return False


def head(fleet, handler) -> None:
    if "method_5xx" in defects(fleet):
        handler.send_error(501, "Unsupported method ('HEAD')")
        return
    _method_not_allowed(fleet, handler, body=False)


def options(fleet, handler) -> None:
    found = defects(fleet)
    if "method_5xx" in found:
        handler.send_error(501, "Unsupported method ('OPTIONS')")
        return
    origin = handler.headers.get("Origin")
    if origin and handler.headers.get("Access-Control-Request-Method"):
        if config(fleet).get("cors") == "allowed":
            handler.send_response(204)
            handler.send_header("Access-Control-Allow-Origin", origin)
            handler.send_header("Access-Control-Allow-Methods", "POST, GET")
            handler.send_header("Access-Control-Allow-Headers", "*")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
    _method_not_allowed(fleet, handler)


def _method_not_allowed(fleet, handler, body: bool = True) -> None:
    payload = json.dumps(
        {"error": {"message": "Method Not Allowed", "type": "invalid_request_error"}}
    ).encode()
    handler.send_response(405)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    if "no_allow_header" not in defects(fleet):
        handler.send_header("Allow", "POST")
    handler.end_headers()
    if body:
        handler.wfile.write(payload)


# ------------------------------------------------------------------ response side


def respond(fleet, handler, code: int, payload: dict) -> dict:
    """Adjust a payload before the fake serializes it."""
    found = defects(fleet)
    path = _path_and_query(handler)[0].rstrip("/")
    if code >= 400 and isinstance(payload.get("error"), dict):
        error = payload["error"]
        if path in ("/v1/messages", "/v1/messages/count_tokens"):
            if "anthropic_error_type" in found:
                error["type"] = "Bad Request"
            elif fleet.anthropic is None:
                error["type"] = ANTHROPIC_TYPES.get(code, error.get("type"))
        elif "openai_error_untyped" in found:
            error.pop("type", None)
    alias = getattr(handler, "llmdv_alias", None)
    if code == 200 and alias and "model" in payload and "rewrite_echoes_served" not in found:
        payload["model"] = alias
    if code == 200 and handler.headers.get("Idempotency-Key") and "id" in payload:
        key = (path, handler.headers["Idempotency-Key"])
        replays = state(fleet)["replays"]
        if not (_replays(fleet) and key in replays):
            payload["id"] = f"{payload['id']}-{uuid.uuid4().hex[:8]}"
            replays.setdefault(key, dict(payload))
    return payload


def _retry_generation(fleet, handler) -> dict | None:
    """The long generation a retried request is served as, when configured."""
    generation = config(fleet).get("retry_generation")
    retried = handler.headers.get("x-stainless-retry-count", "0") != "0"
    return generation if generation and retried else None


def _replays(fleet) -> bool:
    return config(fleet).get("idempotency") == "replayed" or "idempotency_replay" in defects(fleet)


def response_headers(fleet, handler, code: int, body: bytes) -> tuple[bytes, dict]:
    """Content coding, connection and retry headers for a response the fake has built."""
    found = defects(fleet)
    headers: dict[str, str] = {}
    offered = [
        part.split(";")[0].strip().lower()
        for part in (handler.headers.get("Accept-Encoding") or "").split(",")
    ]
    compress = config(fleet).get("compress") and "gzip" in offered
    if (compress or "gzip_always" in found) and body:
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    if "close_connections" in found:
        headers["Connection"] = "close"
        handler.close_connection = True
    if "should_retry_true" in found and 400 <= code < 500:
        headers["x-should-retry"] = "true"
    if "rate_limit_garbled" in found:
        headers["x-ratelimit-remaining-requests"] = "many"
        headers["x-ratelimit-reset-requests"] = "soon"
    return body, headers


def tls_context(fleet, context) -> None:
    protocols = config(fleet).get("alpn")
    if protocols:
        context.set_alpn_protocols(list(protocols))


# ------------------------------------------------------------------ streams


def stream(fleet, handler, body: dict, *, drift=None) -> bool:
    """Serve chat and completions streams with pacing and frame defects. True when served."""
    found = defects(fleet)
    settings = config(fleet)
    path = _path_and_query(handler)[0].rstrip("/")
    completions = path == "/v1/completions"
    generation = None if completions else _retry_generation(fleet, handler)
    paced = settings.get("stream_gap_s") or settings.get("first_event_delay_s") or generation
    if not (completions or paced or settings.get("chat_stream") or settings.get("reasoning_field")):
        if not (STREAM_DEFECTS & found):
            return False
    offered = (handler.headers.get("Accept-Encoding") or "").lower()
    buffered = "buffer_compressed_stream" in found and "gzip" in offered
    if generation:
        time.sleep(generation.get("held_s", 0.0))
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    if buffered:
        handler.send_header("Content-Encoding", "gzip")
    handler.end_headers()
    handler.close_connection = True
    frames = (
        [settings["chat_stream"]]
        if settings.get("chat_stream")
        else _completion_frames(fleet, body, found)
        if completions
        else _chat_frames(fleet, body, found, generation)
    )
    if drift is not None and fleet.contract_defect:
        frames = [drift(fleet.contract_defect, frame.decode()).encode() for frame in frames]
    frames = _frame_defects(frames, found)
    if buffered:
        handler.wfile.write(gzip.compress(b"".join(frames)))
        return True
    gap = settings.get("stream_gap_s") or 0.0
    if generation:
        gap = generation["tokens"] / generation["tokens_per_s"] / (len(frames) - 1)
    # A layer that leaks a cancelled request misbehaves while the generation is still running,
    # so the mark is set for the life of the stream and kept if the client went away.
    leaks = bool({"wedge_after_disconnect", "slow_after_disconnect"} & found)
    if leaks:
        state(fleet)["disconnected"] = True
    try:
        time.sleep(settings.get("first_event_delay_s") or 0.0)
        for index, frame in enumerate(frames):
            handler.wfile.write(frame)
            handler.wfile.flush()
            if gap and index < len(frames) - 1:
                time.sleep(gap)
            if "stall" in found and index == len(frames) // 2:
                time.sleep(1.5)
    except OSError:
        state(fleet)["disconnected"] = True
    else:
        if leaks:
            state(fleet)["disconnected"] = False
    return True


def _sse(value) -> bytes:
    return f"data: {value if isinstance(value, str) else json.dumps(value)}\n\n".encode()


def _frame_defects(frames: list[bytes], found) -> list[bytes]:
    """Line-level defects that apply to any OpenAI-shaped stream."""
    frames = list(frames)
    if "sse_unknown_field" in found:
        frames.insert(1, b"content: stray\n\n")
    if "sse_invalid_utf8" in found:
        at = next(i for i, frame in enumerate(frames) if b'"o"' in frame)
        frames[at] = frames[at].replace(b'"o"', b'"\xff"', 1)
    if "after_done_frame" in found:
        frames.append(frames[-2])
    if "sse_partial_line" in found:
        frames[-1] = frames[-1].rstrip(b"\n")
    if "sentinel_ends_stream" in found:
        frames = [_sentinel_as_finish(frame) for frame in frames]
    return frames


def _sentinel_as_finish(frame: bytes) -> bytes:
    """What LiteLLM 1.105 does: `[DONE]` anywhere in a frame replaces it with a bare finish."""
    if frame == b"data: [DONE]\n\n" or b"[DONE]" not in frame:
        return frame
    payload = json.loads(frame.decode().removeprefix("data: "))
    for choice in payload.get("choices") or []:
        choice["finish_reason"] = "stop"
        if "delta" in choice:
            choice["delta"] = {}
        else:
            choice["text"] = ""
    return _sse(payload)


def _chat_frames(fleet, body: dict, found, generation: dict | None = None) -> list[bytes]:
    settings = config(fleet)
    field = settings.get("reasoning_field") or (
        "reasoning_content" if "reasoning_after_content" in found else None
    )
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    def chunk(delta, finish=None):
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "model": fleet.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    deltas = []
    if "reasoning_after_content" in found:
        deltas += [{field: "weighing"}, {"content": "o"}, {field: " it up"}, {"content": "k"}]
    elif field:
        deltas += [{field: "weighing"}, {field: " it up"}, {"content": "o"}, {"content": "k"}]
    else:
        line = requested_line(body)
        answer = STREAM_ANSWER if line is None else [*STREAM_ANSWER[:4], line]
        deltas += [{"content": piece} for piece in answer]
    if "role_late" in found:
        deltas[1] = {**deltas[1], "role": "assistant"}
        frames = []
    else:
        frames = [_sse(chunk({"role": "assistant", "content": ""}))]
    frames += [_sse(chunk(delta)) for delta in deltas]
    frames.append(_sse(chunk({}, finish="stop")))
    if include_usage:
        tokens = generation["tokens"] if generation else 5
        usage = {"prompt_tokens": 3, "completion_tokens": tokens, "total_tokens": 3 + tokens}
        late = generation and generation.get("usage_choice")
        choices = [{"index": 0, "delta": {}}] if late else []
        frames.append(_sse({**chunk({}), "choices": choices, "usage": usage}))
    frames.append(b"data: [DONE]\n\n")
    return frames


def _completion_frames(fleet, body: dict, found) -> list[bytes]:
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    kind = "chat.completion.chunk" if "completions_chat_object" in found else "text_completion"

    def chunk(text, finish=None):
        return {
            "id": "cmpl-fake",
            "object": kind,
            "model": fleet.model,
            "choices": [{"index": 0, "text": text, "finish_reason": finish}],
        }

    usage = {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}

    def usage_frame():
        return _sse({**chunk(""), "choices": [], "usage": usage})

    frames = []
    if "completions_error_frame" in found:
        frames.append(_sse({"error": {"message": "upstream failed", "type": "server_error"}}))
    if "completions_empty_choice_frame" in found:
        frames.append(_sse({**chunk(""), "choices": [], "usage": None}))
    if fleet.usage_before_finish or (fleet.usage_without_flag and not include_usage):
        frames.append(usage_frame())
    line = requested_line(body)
    frames += [_sse(chunk(piece)) for piece in (("o", "k") if line is None else (line,))]
    if "completions_two_finishes" in found:
        frames.insert(1, _sse(chunk("", finish="length")))
    frames.append(_sse(chunk("", finish="abort" if fleet.abort_finish_reason else "stop")))
    if fleet.two_finish_reasons:
        frames.append(_sse(chunk("", finish="stop")))
    if include_usage and not fleet.usage_before_finish:
        frames.append(usage_frame())
    if not fleet.omit_done:
        frames.append(b"data: [DONE]\n\n")
    return frames


class Redirector:
    """A plain-http listener that redirects every request to https.

    With `redirect=False` it answers `status` instead, as a defect: 200 serves the API over
    plain http, and 503 is a listener with nothing behind it. It also serves as an HTTP proxy, so
    a probe of `http://<any host>` reaches it through HTTP_PROXY; the redirect keeps only the path
    of an absolute-form request target.
    """

    def __init__(self, target: str, redirect: bool = True, status: int = 200) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler
        from urllib.parse import urlsplit

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: A002 - silence the default access log
                pass

            def do_GET(self):
                if redirect:
                    self.send_response(301)
                    path = urlsplit(self.path).path if "://" in self.path else self.path
                    self.send_header("Location", f"{target}{path}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._server.shutdown()
        self._server.server_close()
