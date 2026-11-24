"""What a response reveals about the layers behind it: one table, read the same way at every layer.

A response names the build that produced it (an engine's `system_fingerprint`, a gateway's
version header), sometimes the backend that served it (a router's destination header), and
sometimes a product's own wording. Which header means what is a fact about the product that
sends it, not about where the layer sits, so it lives here once rather than per ingress kind:
every ingress is read against the whole table, and a product in any position contributes
whatever rows it matches. A header that passes through a layer still describes the request, so
a router's destination header seen at the gateway still names the pod that served it.

An operator whose product sends a header this table does not know declares it per ingress
(`ingresses[].version_headers`); a declared header counts as naming the build at that ingress.
So may a header the table publishes without calling it a build, such as `server`, where it is
the only build signal an unknown gateway sends. A name any `backend` row matches, exactly or by
prefix, cannot be declared, because a backend header may name a host or a Service and must never
reach `outcome.json`.

Each row says what it was observed on. A row is added once the header has been seen on a real
response, not from documentation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..http.redact import SECRET_HEADERS


class Reveals(str, Enum):
    # Names a build or a layer's own signature; safe to publish in `outcome.json`.
    VERSION = "version"
    # May name a pod, host or Service: recorded for attribution with the run's evidence, never
    # in `outcome.json` and so never in a comparison, the alert feed or a notification.
    BACKEND = "backend"
    # Kept with the run's evidence, neither a build nor a backend.
    RECORDED = "recorded"
    # Text in a body that identifies the product that wrote it.
    SIGNATURE = "signature"


@dataclass(frozen=True)
class Signal:
    reveals: Reveals
    header: str | None = None
    prefix: str | None = None
    field: str | None = None
    body: str | None = None
    # A value that names a build, so two values in one run mean a rollout in progress and two
    # runs with different values mean the build changed. Routing timings and model names are
    # published evidence but not builds.
    build: bool = False
    # A backend value that names the endpoint or deployment that served the request, so it can
    # be `served_by.backend_pod`. Other backend values (a Service name, an upstream URL) are
    # kept off results and shown only on the run's own dashboard.
    pod: bool = False

    @property
    def name(self) -> str:
        return self.header or self.prefix or self.field or self.body or ""


# Exact headers first, then prefixes, so an exact `backend` row wins over a matching prefix:
# x-llm-d-destination-endpoint names a pod although x-llm-d-* is otherwise published.
SIGNALS: tuple[Signal, ...] = (
    # Every OpenAI-compatible server sends one; vLLM and LiteLLM both send `uvicorn`.
    Signal(Reveals.VERSION, header="server"),
    # vLLM's commit, in the body. It passes through every layer, so it names the engine behind
    # an ingress, not the ingress.
    Signal(Reveals.VERSION, field="system_fingerprint", build=True),
    # Observed on vLLM v0.30.0 and through the llm-d router.
    Signal(Reveals.VERSION, header="x-went-into-resp-headers"),
    # The endpoint that served the request, first: a router's destination names the pod, which
    # is the deepest answer, so it wins over a gateway's deployment id when both pass through.
    # The llm-d router and its Envoy proxy; destination names are best-effort across builds.
    Signal(Reveals.BACKEND, header="x-gateway-destination-endpoint", pod=True),
    Signal(Reveals.BACKEND, header="x-llm-d-destination-endpoint", pod=True),
    Signal(Reveals.BACKEND, header="x-envoy-upstream-host", pod=True),
    # LiteLLM.
    Signal(Reveals.BACKEND, header="x-litellm-model-id", pod=True),
    Signal(Reveals.BACKEND, header="x-litellm-deployment", pod=True),
    Signal(Reveals.BACKEND, header="x-litellm-model-api-base"),
    Signal(Reveals.VERSION, header="x-litellm-version", build=True),
    Signal(Reveals.VERSION, header="x-litellm-model-name"),
    Signal(Reveals.RECORDED, header="x-litellm-call-id"),
    Signal(Reveals.RECORDED, header="x-litellm-response-cost"),
    # Envoy's decorator names the route's Service and namespace.
    Signal(Reveals.BACKEND, header="x-envoy-decorator-operation"),
    # Every variant of the destination headers names an endpoint too (the GIE protocol's
    # x-gateway-destination-endpoint-served, for one), so the families are backend. Listed
    # before the loose prefixes below, which would otherwise take them as published.
    Signal(Reveals.BACKEND, prefix="x-gateway-destination-endpoint"),
    Signal(Reveals.BACKEND, prefix="x-llm-d-destination-endpoint"),
    Signal(Reveals.VERSION, prefix="x-llm-d-"),
    Signal(Reveals.RECORDED, prefix="x-envoy-"),
    Signal(Reveals.RECORDED, prefix="x-gateway-"),
    Signal(Reveals.RECORDED, prefix="x-inference-"),
    Signal(Reveals.RECORDED, prefix="x-epp-"),
    # The llm-d router's own error text: "inference error: <Kind> - ...".
    Signal(Reveals.SIGNATURE, body="inference error:"),
)

BACKEND_HEADERS = frozenset(
    signal.header for signal in SIGNALS if signal.reveals is Reveals.BACKEND and signal.header
)


# Headers `served_by` keeps beside the backend: a layer's quota, and the proxies a response
# passed through (ingress/base.py). Forwarded client addresses and hosts are left out on purpose.
THROTTLE_PREFIXES = ("x-ratelimit-",)
THROTTLE_HEADERS = ("retry-after",)
PROXY_HEADERS = ("via", "x-forwarded-proto")

# Headers that carry a client's or a proxy's address, so they may name a host: forwarded
# chains, a proxy's view of the client, and Envoy's external address.
_HOST_HEADERS = frozenset(
    {
        *PROXY_HEADERS,
        "forwarded",
        "x-real-ip",
        "true-client-ip",
        "cf-connecting-ip",
        "x-envoy-external-address",
    }
)
_HOST_PREFIXES = ("x-forwarded-",)
_HOST_SUFFIXES = ("-client-ip",)

# Headers whose value changes from one request to the next, so it can never name a build: a
# request or trace id, a quota, a time, a length, and llmdv's own correlation headers. Some
# sit under a recorded prefix of the table (`x-envoy-*`), which a declared build header may
# otherwise claim, so they are named here one by one.
_PER_REQUEST_HEADERS = frozenset(
    {
        "request-id",
        "date",
        "content-length",
        "traceparent",
        "tracestate",
        "b3",
        "ratelimit",
        "x-envoy-upstream-service-time",
        *THROTTLE_HEADERS,
    }
)
_PER_REQUEST_PREFIXES = (
    *THROTTLE_PREFIXES,
    "ratelimit-",
    "anthropic-ratelimit-",
    "x-b3-",
    "x-llmdv-",
)
_PER_REQUEST_SUFFIXES = ("-request-id", "-requestid")


def _trace_id(name: str) -> bool:
    """A trace id under any spelling: `x-trace-id`, `x-amzn-trace-id`, `x-b3-traceid`."""
    return "trace" in name and name.endswith("id")


def undeclarable(name: str) -> str | None:
    """Why a header cannot be declared a version header, or None when it can be.

    A declared header names a build: it is published in `outcome.json`, and its values are
    compared between runs, two of them in one run reading as a rollout. So it must be neither a
    credential (`secret`), nor something that may name a host (`host`), nor a value that changes
    with every request (`per_request`). The evidence table's own rows are the loader's to check.
    A recorded prefix row (`x-envoy-*`, `x-gateway-*`) is not refused as a whole: a proxy may
    send its build under one, such as `x-envoy-build`, so only the per-request and address
    headers known under those prefixes are named here.
    """
    name = name.strip().lower()
    if name in SECRET_HEADERS:
        return "secret"
    if name in _HOST_HEADERS or name.startswith(_HOST_PREFIXES) or name.endswith(_HOST_SUFFIXES):
        return "host"
    if (
        name in _PER_REQUEST_HEADERS
        or name.startswith(_PER_REQUEST_PREFIXES)
        or name.endswith(_PER_REQUEST_SUFFIXES)
        or _trace_id(name)
    ):
        return "per_request"
    return None


@dataclass(frozen=True)
class Found:
    # The header, field or marker that matched, and the row it matched: the row's own name, or
    # `<prefix>*` for a prefix row, or the declared header's name.
    signal: str
    reveals: Reveals
    value: str
    build: bool = False
    row: str = ""
    pod: bool = False


def row_label(row: Signal) -> str:
    """How a row is named on the dashboard: a prefix row ends in `*`."""
    return f"{row.prefix}*" if row.prefix else row.name


def row_for(name: str) -> Signal | None:
    """The row a header name matches: an exact row first, else the first matching prefix row."""
    return _row_for(name)


def _row_for(name: str) -> Signal | None:
    exact = next((s for s in SIGNALS if s.header == name), None)
    if exact is not None:
        return exact
    return next((s for s in SIGNALS if s.prefix and name.startswith(s.prefix)), None)


def classify(
    headers: dict[str, str] | None,
    body: Any = None,
    declared: Iterable[str] = (),
    text: str | None = None,
) -> list[Found]:
    """Every signal one response carries, from the table and the ingress's declared headers.

    The result is in a fixed order whatever order the response listed its headers in: table
    rows first, in table order (headers matching one prefix row sorted by name), then declared
    headers in declared order. So `served_by` and every reader built on this give one answer for
    one response. Header names are compared lower-cased. A declared header counts as naming the
    build and wins over a prefix row it also matches; an exact row, and any backend row, always
    wins: the loader refuses declaring one, except a row published without being a build (such
    as `server`), which keeps its row here and counts as a build at that ingress.
    """
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    wanted = list(dict.fromkeys(str(name).lower() for name in declared))
    by_row: dict[int, list[tuple[str, str]]] = {}
    declared_hits: dict[str, str] = {}
    for name, value in lowered.items():
        row = _row_for(name)
        overridable = row is None or (row.prefix is not None and row.reveals is not Reveals.BACKEND)
        if name in wanted and overridable:
            declared_hits[name] = value
        elif row is not None:
            by_row.setdefault(SIGNALS.index(row), []).append((name, value))
    found: list[Found] = []
    searched = None
    for index, row in enumerate(SIGNALS):
        label = row_label(row)
        for name, value in sorted(by_row.get(index, [])):
            found.append(Found(name, row.reveals, value, row.build, label, row.pod))
        if row.field and isinstance(body, dict):
            value = body.get(row.field)
            if isinstance(value, str) and value:
                found.append(Found(row.field, row.reveals, value, row.build, label))
        if row.body:
            searched = _text(body, text) if searched is None else searched
            if row.body in searched:
                found.append(Found(row.body, row.reveals, row.body, row.build, label))
    for name in wanted:
        if name in declared_hits:
            found.append(Found(name, Reveals.VERSION, declared_hits[name], True, name))
    return found


def build_signal_names(declared: Iterable[str] = ()) -> frozenset[str]:
    """Signal names whose values name a build: the table's build rows and declared headers."""
    return frozenset(s.name for s in SIGNALS if s.build) | {str(n).lower() for n in declared}


def _text(body: Any, text: str | None) -> str:
    if body is not None:
        return json.dumps(body, default=str)[:4000]
    return (text or "")[:4000]
