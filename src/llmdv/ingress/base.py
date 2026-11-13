"""Ingresses: transport. Base URL, auth, timeouts, and what a layer reveals about itself.

An ingress knows nothing about request shapes -- that is a surface's job. The split is what
lets one logical case run through vLLM, the router and LiteLLM unchanged, which is the only
reason comparing their outcomes means anything.

`served_by` is the part worth reading twice. Layer attribution assumes the ingresses are in
series and otherwise identical, and they are not: direct vLLM reaches one replica while the
router load-balances across many. In the GLM 5.3 fleets `--reasoning-parser` sits on the
decode container only, so "direct passed, router failed" can mean the gateway is broken, or
that the router picked a replica configured differently from the one that was port-forwarded.
Recording which endpoint served a request is what keeps those two apart, so it is captured
from the first pass even though nothing reads it until `llmdv matrix` exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..http.client import Exchange, RecordingClient
from ..profile.schema import Ingress, IngressKind, ProxyMode

_REGISTRY: dict[IngressKind, IngressAdapter] = {}


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


class IngressAdapter:
    """Per-layer behaviour. Subclasses override only what actually differs."""

    kind: IngressKind
    # Response headers that may name the backend, best-effort and layer-specific.
    backend_headers: tuple[str, ...] = ()
    # Forwarded IP and host values are intentionally excluded because they add sensitive data.
    identity_headers = (
        "server",
        "via",
        "x-forwarded-proto",
    )
    # Response headers and body fields whose value names the build behind this layer, and is
    # safe to publish: a version, a build id, a signature. Never a hostname or an internal
    # service name; those stay in `backend_headers`, which attribution reads and the outcome
    # never records. Bodies pass through every layer, so the engine's fingerprint is a base
    # field, not a vLLM one.
    version_headers: tuple[str, ...] = ("server",)
    version_header_prefixes: tuple[str, ...] = ()
    version_fields: tuple[str, ...] = ("system_fingerprint",)

    def version_signals(self, headers: dict[str, str], body: Any) -> dict[str, str]:
        """The build signals one response carries, by signal name, lower-cased header names."""
        found = {
            name: value
            for name, value in headers.items()
            if (name in self.version_headers or name.startswith(self.version_header_prefixes))
            # A prefix must never pull in a header that names a host or a Service.
            and name not in self.backend_headers
        }
        if isinstance(body, dict):
            for name in self.version_fields:
                value = body.get(name)
                if isinstance(value, str) and value:
                    found[name] = value
        return found

    def served_by(self, exchange: Exchange) -> ServedBy:
        headers = {name.lower(): value for name, value in (exchange.response_headers or {}).items()}
        backend = next(
            (headers[name] for name in self.backend_headers if name in headers),
            None,
        )
        model_echo = None
        if isinstance(exchange.response_body, dict):
            model_echo = exchange.response_body.get("model")
        return ServedBy(
            x_request_id=headers.get("x-request-id"),
            backend_pod=backend,
            model_echo=model_echo,
            extra={
                k: v
                for k, v in headers.items()
                # Throttling signals are evidence whatever the layer: without them a 429 is a
                # bare status with nothing to say how long to wait or what the quota was.
                if k.startswith("x-llm-d")
                or k.startswith("x-ratelimit-")
                or k == "retry-after"
                or k in self.identity_headers
            },
        )


def register(adapter: IngressAdapter) -> IngressAdapter:
    _REGISTRY[adapter.kind] = adapter
    return adapter


def adapter_for(kind: IngressKind) -> IngressAdapter:
    return _REGISTRY.get(kind) or IngressAdapter()


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
