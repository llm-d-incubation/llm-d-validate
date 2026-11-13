"""The llm-d router gateway (EPP + Envoy, or the in-gateway `llm-d-router-gateway` mode).

Failure here with direct vLLM passing points at gateway parsing, mutation, routing or
timeout behaviour -- but only if both layers reached an identically configured replica, which
is why the destination headers below are captured when the build emits them.

The header names are best-effort across router builds. A build that emits none yields
`backend_pod: None`, which is recorded rather than guessed: a wrong backend attribution costs
more than a missing one.
"""

from __future__ import annotations

from ..profile.schema import IngressKind
from .base import IngressAdapter, register


class RouterAdapter(IngressAdapter):
    kind = IngressKind.LLMD_ROUTER
    backend_headers = (
        "x-gateway-destination-endpoint",
        "x-llm-d-destination-endpoint",
        "x-envoy-upstream-host",
    )
    # The router's own signature and its routing headers. Envoy's decorator names a Service.
    version_headers = ("server", "x-went-into-resp-headers")
    version_header_prefixes = ("x-llm-d-",)


ROUTER = register(RouterAdapter())
