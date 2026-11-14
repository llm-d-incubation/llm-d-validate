"""HTTP request handling as clients exercise it: methods, media types, codings, framing, headers.

Each case sends one ordinary request that differs from llmdv's usual request in one HTTP detail
real clients produce: a charset parameter, a gzip body, a chunked body, `Expect: 100-continue`,
a query string, an upper-case header name. Where the spec allows two answers (serve it, or
refuse it with a 4xx), either passes unless the profile pins one under `error_status`; a 5xx, a
hang or a dropped connection never passes. Where the spec requires the request to be served,
the answer must be an ordinary generation.

The probe rides on the first generation surface the ingress exposes, because these are
properties of the layer that every API shares. Every request is a minimal generation or a
GET.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import uuid

import pytest

from ...assertions.errors import assert_client_error, assert_error_envelope
from ...assertions.transport import assert_answered, assert_fast, served_or_refused
from ...evidence import journal
from ..conftest import budget_for, check_sse_response
from .helpers import body, carrier, check_served

SAFE_METHODS = ("HEAD", "OPTIONS")
PROBE_ORIGIN = "https://llmdv-probe.invalid"


@pytest.mark.parametrize("method", SAFE_METHODS)
@pytest.mark.case_id("transport.method_not_allowed")
def test_method_not_allowed(client, model, ingress, method):
    """HEAD and OPTIONS on a POST-only path answer a 4xx, and a 405 says which method works.

    RFC 9110 15.5.6 requires an Allow header on 405. Unsafe methods are deliberately not sent.
    """
    surface = carrier(ingress)
    exchange = client.request(method, surface.path, headers=surface.extra_headers())
    assert_answered(exchange, method)
    declared = ingress.expected_error_status("wrong_method")
    ok = exchange.status in declared if declared else 400 <= exchange.status < 500
    journal.record("status", exchange.status, f"{list(declared) if declared else '4xx'}", ok=ok)
    assert ok, f"{method} {surface.path} answered {exchange.status}"
    if exchange.status == 405:
        allow = {
            item.strip().upper()
            for item in (exchange.response_headers.get("allow") or "").split(",")
            if item.strip()
        }
        journal.record("allow_header", sorted(allow), "lists POST", ok="POST" in allow)
        assert "POST" in allow, "a 405 must carry an Allow header listing POST (RFC 9110 15.5.6)"
    assert_fast(exchange, method)


@pytest.mark.case_id("transport.content_type")
def test_content_type_variants(client, model, ingress):
    """A charset parameter is served; no Content-Type or text/plain is served or refused."""
    surface = carrier(ingress)
    sent = body(surface, model)
    headers = surface.extra_headers()
    exchange = client.post(
        surface.path,
        json_body=sent,
        headers={**headers, "Content-Type": "application/json; charset=utf-8"},
    )
    check_served(exchange, surface, model, ingress)

    raw = json.dumps(sent).encode()
    missing = client.post(
        surface.path, raw_body=raw, content_type=None, headers=headers, record_body=sent
    )
    if served_or_refused(missing, surface, ingress, "missing_content_type", "no_content_type"):
        check_served(missing, surface, model, ingress)
    plain = client.post(
        surface.path, raw_body=raw, content_type="text/plain", headers=headers, record_body=sent
    )
    if served_or_refused(plain, surface, ingress, "wrong_content_type", "text_plain"):
        check_served(plain, surface, model, ingress)


@pytest.mark.case_id("transport.accept_event_stream")
def test_accept_does_not_choose_the_response_shape(client, model, ingress):
    """`stream` decides the response shape: Accept alone never turns JSON into SSE or back."""
    surface = carrier(ingress)
    headers = surface.extra_headers()
    exchange = client.post(
        surface.path,
        json_body=body(surface, model),
        headers={**headers, "Accept": "text/event-stream"},
    )
    content_type = exchange.response_headers.get("content-type", "").split(";")[0].strip()
    journal.record("json_content_type", content_type, "application/json")
    check_served(exchange, surface, model, ingress)

    stream_body = surface.stream_body(
        model.public_id, max_tokens=budget_for(model), include_usage=True
    )
    streamed, transcript = client.stream(
        surface.path,
        json_body=stream_body,
        headers={**headers, "Accept": "application/json"},
        stop_event=surface.stream_stop,
    )
    assert_answered(streamed, "stream_with_json_accept")
    if 400 <= streamed.status < 500:
        journal.record("stream_with_json_accept", streamed.status, "refused", ok=True)
        return
    check_sse_response(streamed, ingress)
    surface.assert_stream(transcript)


@pytest.mark.case_id("transport.request_gzip")
def test_gzip_request_body(client, model, ingress):
    """A gzip body is decoded and served, or refused with a 4xx (415); never a 5xx."""
    surface = carrier(ingress)
    sent = body(surface, model)
    exchange = client.post(
        surface.path,
        raw_body=gzip.compress(json.dumps(sent).encode()),
        headers={**surface.extra_headers(), "Content-Encoding": "gzip"},
        record_body={"content_encoding": "gzip", "decoded": sent},
    )
    if served_or_refused(exchange, surface, ingress, "request_content_encoding", "gzip_body"):
        check_served(exchange, surface, model, ingress)


@pytest.mark.parametrize("offered", ["identity", "gzip", "br", "zstd"])
@pytest.mark.case_id("transport.response_encoding")
def test_response_coding_is_one_the_client_offered(client, model, ingress, offered):
    """A response uses a content coding the client offered, or none (RFC 9110 12.5.3).

    GET /v1/models carries the probe, so no generation is spent on it. A coding llmdv cannot
    decode here (br, zstd) is recorded and not decoded.
    """
    exchange = client.get("/v1/models", headers={"Accept-Encoding": offered})
    assert_answered(exchange, "models")
    coding = (exchange.response_headers.get("content-encoding") or "identity").lower()
    allowed = {"identity", offered}
    ok = coding in allowed
    journal.record("content_encoding", coding, f"one of {sorted(allowed)}", ok=ok)
    assert ok, f"offered only {offered!r} but the response used {coding!r}"
    if coding in ("identity", "gzip"):
        parsed = isinstance(exchange.response_body, dict)
        journal.record("decoded_json", parsed, "a JSON object", ok=parsed)
        assert parsed, "the response did not decode to JSON under its declared coding"


@pytest.mark.case_id("transport.chunked_request")
def test_chunked_request_body(client, model, ingress):
    """A body sent with Transfer-Encoding: chunked is served like one with Content-Length."""
    surface = carrier(ingress)
    exchange = client.post(
        surface.path, json_body=body(surface, model), headers=surface.extra_headers(), chunked=True
    )
    check_served(exchange, surface, model, ingress)


def _body_edges(sent: dict) -> dict[str, tuple[bytes, bool]]:
    """Body edge cases: name -> (bytes, whether RFC 8259 leaves serving it allowed)."""
    encoded = json.dumps(sent).encode()
    first, rest = encoded[1:].split(b",", 1)
    duplicated = b"{" + first + b"," + first + b"," + rest
    nested = b'"llmdv_nested": ' + b"[" * 5000 + b"]" * 5000
    return {
        "empty": (b"", False),
        "byte_order_mark": (b"\xef\xbb\xbf" + encoded, True),
        "duplicate_keys": (duplicated, True),
        "trailing_bytes": (encoded + b" garbage", False),
        "deep_nesting": (encoded[:-1] + b", " + nested + b"}", True),
        "invalid_utf8": (encoded.replace(b'"model"', b'"mo\xffdel"', 1), False),
    }


@pytest.mark.case_id("transport.body_edge")
def test_body_edges(client, model, ingress):
    """JSON bodies at the edges of RFC 8259 get a 4xx or are served; never a 5xx or a hang.

    An empty body, bytes after the value and invalid UTF-8 are not JSON texts and get a 4xx
    with an envelope. A byte order mark, a repeated key and deep nesting may be served or
    refused; a parser that crashes on them answers a 5xx.
    """
    surface = carrier(ingress)
    for name, (raw, may_serve) in _body_edges(body(surface, model)).items():
        exchange = client.post(
            surface.path,
            raw_body=raw,
            headers=surface.extra_headers(),
            record_body=f"<{name}: {len(raw)} bytes>",
        )
        journal.record("body_edge", name)
        if may_serve:
            served_or_refused(exchange, surface, ingress, "malformed_body", name)
        else:
            assert_client_error(exchange, ingress, "malformed_body")
            assert_error_envelope(exchange, surface, ingress, "malformed_body")
            assert_fast(exchange, name)


@pytest.mark.disruptive("sends a request body above the ingress's declared size limit")
@pytest.mark.case_id("transport.body_limit")
def test_body_above_the_declared_limit(client, model, ingress):
    """A body above `transport.max_body_bytes` gets a fast 4xx with an envelope; never a 5xx.

    RFC 9110 15.5.14 names 413 for this; `error_status: {body_too_large: [413]}` pins it. A
    layer that resets the connection or answers 5xx is retried by SDKs, which resend the same
    oversized body.
    """
    limit = ingress.transport.max_body_bytes
    if limit is None:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no transport.max_body_bytes")
    surface = carrier(ingress)
    base = len(json.dumps(body(surface, model, prompt="")).encode())
    padding = " ".join(["word"] * ((limit - base) // 5 + 256))
    sent = body(surface, model, prompt=padding)
    size = len(json.dumps(sent).encode())
    journal.record("body_bytes", size, f"above {limit}", ok=size > limit)
    exchange = client.post(
        surface.path,
        json_body=sent,
        headers=surface.extra_headers(),
        record_body=f"<generation body: {size} bytes>",
    )
    assert_answered(exchange, "oversized_body")
    assert_client_error(exchange, ingress, "body_too_large")
    assert_fast(exchange, "oversized_body")
    assert_error_envelope(exchange, surface, ingress, "body_too_large")


@pytest.mark.case_id("transport.expect_continue")
def test_expect_continue(client, model, ingress):
    """A request carrying Expect: 100-continue, as curl sends for bodies over 1 KiB, is served."""
    surface = carrier(ingress)
    exchange = client.post(
        surface.path,
        json_body=body(surface, model),
        headers={**surface.extra_headers(), "Expect": "100-continue"},
    )
    check_served(exchange, surface, model, ingress)


@pytest.mark.case_id("transport.long_header")
def test_long_header_value(client, model, ingress):
    """A 4 KiB header value, as long trace and session headers reach, is served."""
    surface = carrier(ingress)
    exchange = client.post(
        surface.path,
        json_body=body(surface, model),
        headers={**surface.extra_headers(), "x-llmdv-long-header": "a" * 4096},
    )
    check_served(exchange, surface, model, ingress)


@pytest.mark.case_id("transport.header_name_case")
def test_header_names_are_case_insensitive(client, model, ingress):
    """Required headers work in upper case (RFC 9110 5.1).

    The surface's own required headers, Content-Type and the credential header are all sent
    with upper-case names, so a layer that matches one of them case-sensitively fails.
    """
    surface = carrier(ingress, prefer=("messages",))
    upper = {name.upper(): value for name, value in surface.extra_headers().items()}
    upper["CONTENT-TYPE"] = "application/json"
    for name, value in client.default_headers.items():
        if name.lower() in ("authorization", "x-api-key"):
            upper[name.upper()] = value
    journal.record("upper_case_headers", sorted(upper))
    exchange = client.post(surface.path, json_body=body(surface, model), headers=upper)
    check_served(exchange, surface, model, ingress)


@pytest.mark.case_id("transport.header_underscore")
def test_header_name_with_underscore(client, model, ingress):
    """A header name with an underscore is served, or refused with a declared 4xx; never 5xx.

    Older Codex releases send `session_id`. RFC 9110 allows the name; proxies may drop it.
    """
    surface = carrier(ingress)
    exchange = client.post(
        surface.path,
        json_body=body(surface, model),
        headers={**surface.extra_headers(), "session_id": "llmdv-probe"},
    )
    if served_or_refused(exchange, surface, ingress, "header_underscore", "underscore_header"):
        check_served(exchange, surface, model, ingress)


# What the pinned SDKs and coding agents send on every request, including a retried one.
CLIENT_HEADERS = {
    "User-Agent": "OpenAI/Python 3.11.0",
    "x-stainless-lang": "python",
    "x-stainless-package-version": "3.11.0",
    "x-stainless-os": "Linux",
    "x-stainless-arch": "x64",
    "x-stainless-runtime": "CPython",
    "x-stainless-runtime-version": "3.12.0",
    "x-stainless-retry-count": "1",
    "x-stainless-read-timeout": "600",
    "x-stainless-timeout": "600",
    "x-session-id": "llmdv-probe",
    "x-claude-code-session-id": "llmdv-probe",
    "conversation-id": "llmdv-probe",
}


@pytest.mark.case_id("transport.model_rewrite_header")
def test_model_rewrite_header(client, model, ingress):
    """A client model name with the declared rewrite header is served, and echoed back.

    The header names the served model; the client keeps seeing the name it sent.
    """
    header = ingress.transport.model_rewrite_header
    if header is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no transport.model_rewrite_header"
        )
    surface = carrier(ingress)
    alias = dataclasses.replace(model, public_id=f"llmdv-alias-{uuid.uuid4().hex[:8]}")
    exchange = client.post(
        surface.path,
        json_body=body(surface, alias),
        headers={**surface.extra_headers(), header: model.public_id},
    )
    check_served(exchange, surface, alias, ingress)


@pytest.mark.case_id("transport.client_headers")
def test_client_and_sdk_headers(client, model, ingress):
    """The fixed headers SDKs and coding agents send are served, a retry count above zero too."""
    surface = carrier(ingress)
    journal.record("client_headers", sorted(CLIENT_HEADERS))
    exchange = client.post(
        surface.path,
        json_body=body(surface, model),
        headers={**surface.extra_headers(), **CLIENT_HEADERS},
    )
    check_served(exchange, surface, model, ingress)


@pytest.mark.case_id("transport.query_string")
def test_query_string_on_an_api_path(client, model, ingress):
    """An API path with a query string is served like the bare path.

    Claude Code sends `/v1/messages?beta=true`, so Messages carries the probe where exposed.
    """
    surface = carrier(ingress, prefer=("messages",))
    exchange = client.post(
        f"{surface.path}?beta=true", json_body=body(surface, model), headers=surface.extra_headers()
    )
    check_served(exchange, surface, model, ingress)


PATH_VARIANTS = {
    "trailing_slash": lambda path: f"{path}/",
    "double_slash": lambda path: path.replace("/v1/", "/v1//", 1),
    "encoded_slash": lambda path: "/v1/" + path[len("/v1/") :].replace("/", "%2F"),
}


@pytest.mark.parametrize("variant", sorted(PATH_VARIANTS))
@pytest.mark.case_id("transport.path_variant")
def test_path_variant(client, model, ingress, variant):
    """A slash variant of an API path is served, redirected or refused, quickly; never 5xx."""
    surface = carrier(ingress)
    path = PATH_VARIANTS[variant](surface.path)
    journal.record("path", path)
    exchange = client.post(path, json_body=body(surface, model), headers=surface.extra_headers())
    assert_answered(exchange, variant)
    declared = ingress.expected_error_status("path_variant")
    if declared:
        assert_client_error(exchange, ingress, "path_variant")
    elif 200 <= exchange.status < 300:
        check_served(exchange, surface, model, ingress)
    assert_fast(exchange, variant)


@pytest.mark.case_id("transport.cors_preflight")
def test_cors_preflight(client, model, ingress):
    """A browser preflight is answered as declared: allowed for any origin, or refused."""
    declared = ingress.transport.cors_preflight
    if declared is None:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no transport.cors_preflight")
    surface = carrier(ingress)
    exchange = client.request(
        "OPTIONS",
        surface.path,
        headers={
            "Origin": PROBE_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, authorization",
        },
    )
    assert_answered(exchange, "preflight")
    allowed_origin = exchange.response_headers.get("access-control-allow-origin")
    allows = 200 <= exchange.status < 300 and allowed_origin in (PROBE_ORIGIN, "*")
    journal.record(
        "preflight",
        {"status": exchange.status, "allow_origin": allowed_origin},
        declared,
        ok=allows == (declared == "allowed"),
    )
    assert allows == (declared == "allowed"), (
        f"preflight is declared {declared!r} but answered {exchange.status} with "
        f"Access-Control-Allow-Origin {allowed_origin!r}"
    )
