"""The adapter table reproduces the per-kind adapter classes it replaced, byte for byte.

`_Old*` below are those classes, frozen as they were. Each response header set is read by the
old class and by the table row for the same kind; `served_by` (including the order of its
`extra` keys, which reaches the recorded JSON) and `version_signals` must match exactly.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import pytest

from llmdv.http.client import Exchange
from llmdv.ingress import ADAPTERS, IngressAdapter, ServedBy, adapter_for
from llmdv.profile.schema import IngressKind


class _OldBase:
    backend_headers: tuple[str, ...] = ()
    identity_headers = ("server", "via", "x-forwarded-proto")
    version_headers: tuple[str, ...] = ("server",)
    version_header_prefixes: tuple[str, ...] = ()
    version_fields: tuple[str, ...] = ("system_fingerprint",)

    def version_signals(self, headers: dict[str, str], body: Any) -> dict[str, str]:
        found = {
            name: value
            for name, value in headers.items()
            if (name in self.version_headers or name.startswith(self.version_header_prefixes))
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
        backend = next((headers[name] for name in self.backend_headers if name in headers), None)
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
                if k.startswith("x-llm-d")
                or k.startswith("x-ratelimit-")
                or k == "retry-after"
                or k in self.identity_headers
            },
        )


class _OldVllm(_OldBase):
    backend_headers = ()


class _OldRouter(_OldBase):
    backend_headers = (
        "x-gateway-destination-endpoint",
        "x-llm-d-destination-endpoint",
        "x-envoy-upstream-host",
    )
    version_headers = ("server", "x-went-into-resp-headers")
    version_header_prefixes = ("x-llm-d-",)


class _OldLiteLLM(_OldBase):
    backend_headers = ("x-litellm-model-id", "x-litellm-deployment")
    version_headers = ("server", "x-litellm-version", "x-litellm-model-name")

    def served_by(self, exchange: Exchange) -> ServedBy:
        base = super().served_by(exchange)
        headers = {name.lower(): value for name, value in (exchange.response_headers or {}).items()}
        extra = dict(base.extra)
        extra.update({k: v for k, v in headers.items() if k.startswith("x-ratelimit-")})
        for name in ("x-litellm-call-id", "x-litellm-response-cost"):
            if name in headers:
                extra[name] = headers[name]
        return ServedBy(
            x_request_id=base.x_request_id,
            backend_pod=base.backend_pod,
            model_echo=base.model_echo,
            extra=extra,
        )


OLD = {
    IngressKind.VLLM: _OldVllm(),
    IngressKind.LLMD_ROUTER: _OldRouter(),
    IngressKind.LITELLM: _OldLiteLLM(),
}

# Header groups, each one a thing some adapter treats specially. Every combination of up to
# three groups, in both orders, is a header set; mixed case checks the lower-casing.
GROUPS: dict[str, dict[str, str]] = {
    "gateway_destination": {"x-gateway-destination-endpoint": "10.0.0.1:8000"},
    "llmd_destination": {"X-LLM-D-Destination-Endpoint": "10.0.0.2:8000"},
    "envoy_upstream": {"x-envoy-upstream-host": "10.0.0.3:8000"},
    "litellm_model_id": {"x-litellm-model-id": "abc123"},
    "litellm_deployment": {"x-litellm-deployment": "deploy-a"},
    "llmd_version": {"x-llm-d-router-version": "v0.4.0", "x-llm-d-epp": "epp-1"},
    "llmd_no_dash": {"x-llm-dx": "odd"},
    "ratelimit": {"x-ratelimit-remaining-requests": "9", "X-RateLimit-Limit-Tokens": "1000"},
    "retry_after": {"Retry-After": "3"},
    "litellm_ids": {"x-litellm-call-id": "call-1", "x-litellm-response-cost": "0.002"},
    "litellm_version": {"x-litellm-version": "1.50.0", "x-litellm-model-name": "m"},
    "identity": {"Server": "uvicorn", "via": "1.1 proxy", "x-forwarded-proto": "https"},
    "went_into": {"x-went-into-resp-headers": "true"},
    "request_id": {"x-request-id": "req-1"},
    "unrelated": {"content-type": "application/json", "x-forwarded-for": "10.1.1.1"},
}

HEADER_SETS: list[dict[str, str] | None] = [None, {}]
for size in (1, 2, 3):
    for combo in itertools.combinations(GROUPS, size):
        for ordered in (combo, tuple(reversed(combo))) if size > 1 else (combo,):
            merged: dict[str, str] = {}
            for name in ordered:
                merged.update(GROUPS[name])
            HEADER_SETS.append(merged)
HEADER_SETS.append({k: v for group in GROUPS.values() for k, v in group.items()})

BODIES: list[Any] = [
    None,
    {"model": "m1", "system_fingerprint": "fp-1"},
    {"model": "m1", "system_fingerprint": ""},
    ["not", "a", "dict"],
]


def _exchange(headers: dict[str, str] | None, body: Any) -> Exchange:
    return Exchange(
        method="POST", url="http://unused", status=200, response_headers=headers, response_body=body
    )


def _dump(value: Any) -> str:
    # Key order included: the recorded JSON carries it.
    return json.dumps(value)


@pytest.mark.parametrize("kind", list(IngressKind), ids=lambda kind: kind.value)
def test_the_table_row_reads_every_response_as_the_old_class_did(kind):
    old, new = OLD[kind], adapter_for(kind)
    for headers, body in itertools.product(HEADER_SETS, BODIES):
        exchange = _exchange(headers, body)
        assert _dump(new.served_by(exchange).to_dict()) == _dump(
            old.served_by(exchange).to_dict()
        ), headers
        lowered = {k.lower(): v for k, v in (headers or {}).items()}
        assert _dump(new.version_signals(lowered, body)) == _dump(
            old.version_signals(lowered, body)
        ), headers


def test_every_kind_has_a_row_and_an_unknown_kind_reads_as_the_base():
    assert set(ADAPTERS) == set(IngressKind)
    base = _OldBase()
    for headers, body in itertools.product(HEADER_SETS, BODIES):
        exchange = _exchange(headers, body)
        assert _dump(IngressAdapter().served_by(exchange).to_dict()) == _dump(
            base.served_by(exchange).to_dict()
        )
