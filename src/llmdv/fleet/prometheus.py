"""A read-only Prometheus HTTP API client: GET /api/v1/query and nothing else.

Like the kubectl adapter, the guarantee is structural. The client has one public method, the
path is a constant, and the method is GET; there is no code path to the admin API, remote
write or any other endpoint, so a metrics case cannot change the monitoring system it reads.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import httpx

from ..http import redact
from ..log.setup import get_logger
from ..profile.fleet import MetricsSource

log = get_logger("fleet.prometheus")

QUERY_PATH = "/api/v1/query"


class MetricsError(Exception):
    """The source did not answer a query. `kind` says whose problem that is."""

    def __init__(self, message: str, kind: str = "unreachable") -> None:
        super().__init__(message)
        # unreachable: transport, timeout or 5xx -- the vantage point's problem.
        # rejected: the source refused the query itself (400 bad_data, 422 execution) -- the
        #   declaration's problem.
        # denied: 401 or 403 -- the credential's problem.
        # partial: an answer carrying warnings, such as a federated querier with a store down.
        #   Missing series read as zero or as fewer endpoints, so the data proves nothing.
        self.kind = kind


@dataclass(frozen=True)
class Sample:
    labels: dict[str, str] = field(default_factory=dict)
    value: float = math.nan


def token_for(source: MetricsSource) -> str | None:
    """The declared bearer token, registered for redaction before anything can log it."""
    if source.auth != "bearer" or not source.key_env:
        return None
    token = os.environ.get(source.key_env) or None
    if token:
        redact.install_secrets([token])
    return token


class PrometheusReader:
    def __init__(self, source: MetricsSource, token: str | None = None) -> None:
        self.source = source
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def describe(self) -> str:
        return f"{self.source.url}{QUERY_PATH}"

    def query(self, expr: str) -> list[Sample]:
        """One instant query. A vector or a scalar comes back as samples; nothing else does."""
        try:
            with httpx.Client(
                timeout=self.source.timeout,
                trust_env=self.source.proxy == "allow",
                verify=self.source.tls_verify,
            ) as client:
                response = client.get(
                    self.source.url + QUERY_PATH, params={"query": expr}, headers=self._headers
                )
        except httpx.HTTPError as exc:
            raise MetricsError(redact.scrub(f"{type(exc).__name__}: {exc}")) from exc
        log.debug("metrics query %s -> %s", expr, response.status_code)
        if response.status_code in (401, 403):
            raise MetricsError(f"HTTP {response.status_code}", kind="denied")
        try:
            payload = response.json()
        except ValueError:
            raise MetricsError(f"HTTP {response.status_code}, body is not JSON") from None
        if not isinstance(payload, dict):
            raise MetricsError(f"HTTP {response.status_code}, body is not an object")
        if payload.get("status") != "success":
            kind = "rejected" if response.status_code in (400, 422) else "unreachable"
            detail = f"{payload.get('errorType')}: {payload.get('error')}"
            raise MetricsError(redact.scrub(f"HTTP {response.status_code}, {detail}"), kind=kind)
        warnings = payload.get("warnings")
        if warnings:
            raise MetricsError(redact.scrub(f"partial answer: {warnings}"[:500]), kind="partial")
        data = payload.get("data") or {}
        result = data.get("result")
        if data.get("resultType") == "scalar" and isinstance(result, list):
            return [Sample({}, _value(result))]
        if data.get("resultType") != "vector" or not isinstance(result, list):
            raise MetricsError(
                f"expected an instant vector, got {data.get('resultType')!r}", kind="rejected"
            )
        return [
            Sample(
                {str(k): str(v) for k, v in (item.get("metric") or {}).items()},
                _value(item.get("value")),
            )
            for item in result
            if isinstance(item, dict)
        ]


def _value(pair) -> float:
    try:
        return float(pair[1])
    except (TypeError, ValueError, IndexError):
        return math.nan
