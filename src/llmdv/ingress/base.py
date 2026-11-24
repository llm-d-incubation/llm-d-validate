"""Ingresses: transport. Base URL, auth, timeouts, and what a layer reveals about itself.

An ingress knows nothing about request shapes -- that is a surface's job. The split is what
lets one logical case run through vLLM, the router and LiteLLM unchanged, which is the only
reason comparing their outcomes means anything.

`served_by` is the part worth reading twice. Layer attribution assumes the ingresses are in
series and otherwise identical, and they are not: direct vLLM reaches one replica while the
router load-balances across many. Engine flags such as `--reasoning-parser` can differ per
container (on the decode container only, for example), so "direct passed, router failed" can
mean the gateway is broken, or that the router picked a replica configured differently from
the one that was port-forwarded.
Recording which endpoint served a request is what keeps those two apart, so it is captured
from the first pass even though nothing reads it until `llmdv matrix` exists.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..http.client import Exchange, RecordingClient
from ..profile.schema import Ingress, ProxyMode
from . import signals


@dataclass(frozen=True)
class ServedBy:
    """Which backend answered, as far as this layer admits.

    Every field is optional: a layer that reveals nothing yields an all-None record, which is
    itself worth knowing when attribution later turns out to be ambiguous.
    """

    x_request_id: str | None = None
    backend_pod: str | None = None
    model_echo: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "x_request_id": self.x_request_id,
            "backend_pod": self.backend_pod,
            "model_echo": self.model_echo,
            **({"extra": self.extra} if self.extra else {}),
        }


# Recorded on every layer, whatever product answered. Throttling signals are evidence wherever
# they appear: without them a 429 is a bare status with nothing to say how long to wait or what
# the quota was. Forwarded IP and host values are left out because they add sensitive data.
THROTTLE_PREFIXES = ("x-ratelimit-",)
THROTTLE_HEADERS = ("retry-after",)
PROXY_HEADERS = ("via", "x-forwarded-proto")


def version_signals(
    headers: dict[str, str], body: Any, declared: Iterable[str] = ()
) -> dict[str, str]:
    """The published signals one response carries (the table's `version` rows and the headers
    the ingress declares), by signal name: never one that may name a host or a Service."""
    return {
        found.signal: found.value
        for found in signals.classify(headers, body, declared)
        if found.reveals is signals.Reveals.VERSION
    }


def served_by(exchange: Exchange, declared: Iterable[str] = ()) -> ServedBy:
    """Which backend answered, as far as the response admits, read the same way at every layer.

    `backend_pod` is the first row, in table order, that names the endpoint or deployment that
    served the request; a header that passed through from a deeper layer still names it. Any
    other such value is kept in `extra`, so a gateway's deployment id is not lost when a
    router's destination is also present. A backend value that names a Service or an upstream
    URL is never recorded here, nor a recorded-only header family (`x-envoy-*`, ...), which the
    run's dashboard shows from the exchanges. Everything is in a fixed order, so one response
    gives one record whatever order it listed its headers in.
    """
    headers = {name.lower(): value for name, value in (exchange.response_headers or {}).items()}
    found = signals.classify(headers, None, declared)
    pods = [item for item in found if item.pod]
    backend = pods[0].value if pods else None
    model_echo = None
    if isinstance(exchange.response_body, dict):
        model_echo = exchange.response_body.get("model")
    extra = {
        item.signal: item.value
        for item in found
        if item.reveals is signals.Reveals.VERSION
        or (item.reveals is signals.Reveals.RECORDED and not item.row.endswith("*"))
    }
    for item in pods[1:]:
        extra[item.signal] = item.value
    for name, value in sorted(headers.items()):
        if name.startswith(THROTTLE_PREFIXES) or name in THROTTLE_HEADERS or name in PROXY_HEADERS:
            extra[name] = value
    return ServedBy(
        x_request_id=headers.get("x-request-id"),
        backend_pod=backend,
        model_echo=model_echo,
        extra=extra,
    )


def client_for(
    ingress: Ingress,
    secret: str | None = None,
    extra_headers: dict[str, str] | None = None,
    request_id_factory: Callable[[], str] | None = None,
    correlation_headers: dict[str, str] | None = None,
    transcript_cap: int | None = None,
) -> RecordingClient:
    """Build a recording client bound to one ingress.

    `trust_env` follows the profile: `proxy: bypass` means httpx must not inherit HTTP_PROXY
    from the environment, because a silent proxy hop is a confounder rather than an error.
    """
    # Surface and case-specific headers deliberately win over correlation defaults. A probe
    # may need to send one of these names with a value of its own.
    headers = {
        **ingress.auth.header(secret),
        **(correlation_headers or {}),
        **(extra_headers or {}),
    }
    if request_id_factory is not None:
        headers.setdefault("x-request-id", request_id_factory())
    return RecordingClient(
        base_url=ingress.base_url,
        default_headers=headers,
        connect_timeout=ingress.timeouts.connect,
        request_timeout=ingress.timeouts.request,
        trust_env=ingress.setup.proxy is ProxyMode.ALLOW,
        verify=ingress.tls.verify,
        # stream_idle has been in every profile since the schema landed and was read by
        # nothing until streaming existed. A stalled decode is a different failure from a slow
        # one, and this is what tells them apart.
        stream_idle=ingress.timeouts.stream_idle,
        **({} if transcript_cap is None else {"transcript_cap": transcript_cap}),
    )
