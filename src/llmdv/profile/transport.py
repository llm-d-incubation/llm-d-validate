"""Per-ingress HTTP behavior that legitimately differs between layers.

Declared under an ingress's `transport:` mapping. Every field is optional and its default keeps
the verdicts of a profile written before this mapping existed: a case that needs a field skips
as `undeclared` without it, and a case that has a safe default (served or refused with a 4xx,
never a 5xx) uses that default.

Exposure declarations name the paths a case may GET. Engine admin and dev-mode routes can never
be declared, because no case may call them, read-only or not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Any
from urllib.parse import urlsplit

# Error classes for transport-level requests. A case without a declared status for its class
# applies the documented default named in its docstring.
TRANSPORT_ERROR_CLASSES = frozenset(
    {
        "missing_content_type",
        "wrong_content_type",
        "request_content_encoding",
        "path_variant",
        "header_underscore",
        "malformed_body",
        "empty_input",
        "invalid_role",
        "tool_choice_without_tools",
        "missing_model",
        "token_limit_over_context",
        "missing_token_limit",
        "invalid_image",
        "image_input",
        "invalid_thinking_type",
        "server_tool",
        "sampling_option",
        "body_too_large",
    }
)

CORS_PREFLIGHT = ("allowed", "refused")
IDEMPOTENCY = ("replayed", "ignored")
ALPN_PROTOCOLS = ("h2", "http/1.1")
# `http_redirect`: the only declarable answer of the ingress's own host over plain http.
HTTP_REDIRECT = ("expected",)
# Alternate credential headers a gateway may accept. Each is already redacted as a secret.
CREDENTIAL_HEADERS = ("x-api-key", "api-key")

# Engine routes that change state or expose internals to an operator. A declaration naming one
# is refused at load time, so no case can send a request there.
ADMIN_PATHS = frozenset(
    {
        "/abort_requests",
        "/collective_rpc",
        "/finish_weight_update",
        "/get_world_size",
        "/init_weight_transfer_engine",
        "/is_paused",
        "/is_scaling_elastic_ep",
        "/is_sleeping",
        "/kv_event_sources",
        "/pause",
        "/release_kv_cache_memory",
        "/reset_encoder_cache",
        "/reset_mm_cache",
        "/reset_prefix_cache",
        "/resume",
        "/scale_elastic_ep",
        "/server_info",
        "/sleep",
        "/start_draft_weight_update",
        "/start_profile",
        "/start_weight_update",
        "/stop_profile",
        "/update_weight_version",
        "/update_weights",
        "/wake_up",
        "/weight_info",
    }
)


class TransportError(ValueError):
    """A malformed `transport` declaration."""


@dataclass(frozen=True)
class StreamBudget:
    """Timing a long stream must meet. An absent bound is not checked."""

    first_event_s: float | None = None
    max_gap_s: float | None = None
    min_tokens_per_s: float | None = None
    next_request_s: float | None = None


@dataclass(frozen=True)
class TransportContract:
    # Statuses GET health_path must answer.
    health_status: tuple[int, ...] = ()
    # Paths a GET must answer with a 2xx, and paths a GET must answer with `absent_status`.
    present_paths: tuple[str, ...] = ()
    absent_paths: tuple[str, ...] = ()
    absent_status: tuple[int, ...] = (403, 404)
    # A plain-http base URL that must redirect to https: a listener somewhere other than the
    # ingress's own host, so in practice only in a local profile.
    http_url: str | None = None
    # "expected": `http://` plus the host of the ingress's https base URL must redirect to https.
    # The URL is derived at run time, so a committed profile names no hostname.
    http_redirect: str | None = None
    cors_preflight: str | None = None
    # The ALPN protocol the ingress selects when a client offers both h2 and http/1.1.
    alpn: str | None = None
    # None: undeclared. A list, possibly empty, names every alternate header the gateway accepts.
    credential_headers: tuple[str, ...] | None = None
    model_rewrite_header: str | None = None
    idempotency: str | None = None
    stream_budget: StreamBudget | None = None
    max_body_bytes: int | None = None


def parse(raw: Any, where: str) -> TransportContract:
    """Parse one ingress's `transport` mapping, naming the field at fault."""
    if raw is None:
        return TransportContract()
    _known_keys(raw, TransportContract, where)
    if raw.get("http_url") is not None and raw.get("http_redirect") is not None:
        raise TransportError(f"{where}: declare http_url or http_redirect, not both")
    return TransportContract(
        health_status=_statuses(raw.get("health_status", []), f"{where}.health_status"),
        present_paths=_paths(raw.get("present_paths", []), f"{where}.present_paths"),
        absent_paths=_paths(raw.get("absent_paths", []), f"{where}.absent_paths"),
        absent_status=_statuses(raw.get("absent_status", [403, 404]), f"{where}.absent_status"),
        http_url=_http_url(raw.get("http_url"), f"{where}.http_url"),
        http_redirect=_choice(raw.get("http_redirect"), HTTP_REDIRECT, f"{where}.http_redirect"),
        cors_preflight=_choice(
            raw.get("cors_preflight"), CORS_PREFLIGHT, f"{where}.cors_preflight"
        ),
        alpn=_choice(raw.get("alpn"), ALPN_PROTOCOLS, f"{where}.alpn"),
        credential_headers=None
        if raw.get("credential_headers") is None
        else tuple(
            _choice(item, CREDENTIAL_HEADERS, f"{where}.credential_headers[{index}]")
            for index, item in enumerate(_list(raw["credential_headers"], where))
        ),
        model_rewrite_header=_header(
            raw.get("model_rewrite_header"), f"{where}.model_rewrite_header"
        ),
        idempotency=_choice(raw.get("idempotency"), IDEMPOTENCY, f"{where}.idempotency"),
        stream_budget=_budget(raw.get("stream_budget"), f"{where}.stream_budget"),
        max_body_bytes=_size(raw.get("max_body_bytes"), f"{where}.max_body_bytes"),
    )


def _known_keys(raw: Any, cls: type, where: str) -> None:
    if not isinstance(raw, dict):
        raise TransportError(f"{where}: expected a mapping, got {raw!r}")
    names = {item.name for item in fields(cls)}
    for name in raw:
        if name not in names:
            raise TransportError(f"{where}.{name}: unknown field")


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise TransportError(f"{where}: expected a list, got {value!r}")
    return list(value)


def _statuses(value: Any, where: str) -> tuple[int, ...]:
    out = []
    for index, code in enumerate(_list(value, where)):
        if type(code) is not int or not 100 <= code <= 599:
            raise TransportError(f"{where}[{index}]: expected an HTTP status, got {code!r}")
        out.append(code)
    return tuple(out)


def _paths(value: Any, where: str) -> tuple[str, ...]:
    out = []
    for index, path in enumerate(_list(value, where)):
        at = f"{where}[{index}]"
        if not isinstance(path, str) or not path.startswith("/") or any(c.isspace() for c in path):
            raise TransportError(f"{at}: expected a path starting with /, got {path!r}")
        if path.split("?", 1)[0].rstrip("/") in ADMIN_PATHS:
            raise TransportError(f"{at}: {path} is an engine admin route, which no case may call")
        out.append(path)
    return tuple(out)


def _http_url(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("http://"):
        raise TransportError(f"{where}: expected a URL starting with http://, got {value!r}")
    return value.rstrip("/")


def plain_http_url(base_url: str) -> tuple[str | None, str | None]:
    """The plain-http address `http_redirect` probes for `base_url`, or why there is none.

    It is `http://` plus the base URL's host, on the default port: the address a user who types
    the gateway's name without a scheme reaches. Only an https base URL on the default port has
    one. An `http://` base URL is already plain, so there is nothing to redirect (an internal
    layer reached over plain HTTP, such as a port-forward, does not declare the flag). An
    explicit non-default port says nothing about where a plain listener would be, and guessing
    it would probe some other service, so that is refused too; declare `http_url` instead.

    The reason never quotes the URL or the host: a committed profile takes the base URL from a
    variable precisely so no hostname is written down.
    """
    try:
        parts = urlsplit(base_url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None, "transport.http_redirect needs an https base URL with a valid port"
    if parts.scheme.lower() != "https" or not host:
        return None, "transport.http_redirect needs an https base URL"
    if port not in (None, 443):
        return None, (
            f"transport.http_redirect derives the plain address only for an https base URL on "
            f"the default port, and this one names port {port}; declare transport.http_url "
            f"instead"
        )
    return f"http://[{host}]" if ":" in host else f"http://{host}", None


def _choice(value: Any, allowed: tuple[str, ...], where: str) -> str | None:
    if value is None:
        return None
    if value not in allowed:
        raise TransportError(f"{where}: expected one of {list(allowed)}, got {value!r}")
    return value


def _header(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or any(c.isspace() or c == ":" for c in value):
        raise TransportError(f"{where}: expected a header name, got {value!r}")
    from ..http.redact import SECRET_HEADERS

    if value.lower() in SECRET_HEADERS:
        raise TransportError(f"{where}: expected a non-credential header")
    return value.lower()


def _budget(raw: Any, where: str) -> StreamBudget | None:
    if raw is None:
        return None
    _known_keys(raw, StreamBudget, where)
    values = {}
    for name, value in raw.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise TransportError(f"{where}.{name}: expected a positive number, got {value!r}")
        values[name] = float(value)
    return StreamBudget(**values)


def _size(raw: Any, where: str) -> int | None:
    if raw is None:
        return None
    if type(raw) is not int or raw <= 0:
        raise TransportError(f"{where}: expected a positive integer byte count, got {raw!r}")
    return raw
