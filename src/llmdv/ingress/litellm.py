"""LiteLLM proxy.

Failure here with the router passing points at LiteLLM mapping, transformation, auth or
timeout behaviour. It is also the only layer in a typical stack that authenticates, so a
401 here is a credential problem rather than a fleet problem.

LiteLLM is relatively forthcoming about itself: it names the deployment it selected and, with
the v3 limiter active, mirrors rate-limit state into `x-ratelimit-*` -- the same statuses the
flow-control hook reads for its saturation signal. Those are captured so a `promised`
rate-limit-header declaration can be verified from the response alone.
"""

from __future__ import annotations

from ..http.client import Exchange
from ..profile.schema import IngressKind
from .base import IngressAdapter, ServedBy, register

RATELIMIT_PREFIX = "x-ratelimit-"


class LiteLLMAdapter(IngressAdapter):
    kind = IngressKind.LITELLM
    backend_headers = ("x-litellm-model-id", "x-litellm-deployment")
    version_headers = ("server", "x-litellm-version", "x-litellm-model-name")

    def served_by(self, exchange: Exchange) -> ServedBy:
        base = super().served_by(exchange)
        headers = {name.lower(): value for name, value in (exchange.response_headers or {}).items()}
        extra = dict(base.extra)
        extra.update({k: v for k, v in headers.items() if k.startswith(RATELIMIT_PREFIX)})
        for name in ("x-litellm-call-id", "x-litellm-response-cost"):
            if name in headers:
                extra[name] = headers[name]
        return ServedBy(
            x_request_id=base.x_request_id,
            backend_pod=base.backend_pod,
            model_echo=base.model_echo,
            extra=extra,
        )


LITELLM = register(LiteLLMAdapter())
