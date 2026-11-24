"""What a response reveals is read from one table, the same way at every layer.

`_Old*` below are the per-kind adapter classes the table replaced, frozen as they were. They are
the oracle for one guarantee: nothing any of them recorded is lost, whichever position the layer
sits at. The rest pins the table's own rules: a backend header is never published, an exact
backend row wins over a matching prefix, and an operator's declared header counts as a build.
"""

from __future__ import annotations

import itertools
import json
import random
from typing import Any

import pytest

from llmdv.http.client import Exchange
from llmdv.ingress import ServedBy, served_by, version_signals
from llmdv.ingress import signals as table
from llmdv.profile import loader
from llmdv.profile.errors import ProfileError
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
    IngressKind.ENGINE: _OldVllm(),
    IngressKind.ROUTER: _OldRouter(),
    IngressKind.GATEWAY: _OldLiteLLM(),
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


@pytest.mark.parametrize("kind", list(IngressKind), ids=lambda kind: kind.value)
def test_nothing_an_old_adapter_recorded_is_lost(kind):
    """Every signal the old class published is still published, every `extra` key it kept is
    still kept, and every backend value it recorded is still recorded, as `backend_pod` or in
    `extra` when a deeper layer's endpoint is also present."""
    old = OLD[kind]
    for headers, body in itertools.product(HEADER_SETS, BODIES):
        lowered = {k.lower(): v for k, v in (headers or {}).items()}
        published = version_signals(lowered, body)
        for name, value in old.version_signals(lowered, body).items():
            assert published.get(name) == value, (name, headers)
        new = served_by(_exchange(headers, body))
        old_served = old.served_by(_exchange(headers, body))
        for name, value in old_served.extra.items():
            if name == "x-llm-dx":
                # The one deliberate narrowing: the old rule kept any header starting
                # `x-llm-d`, dash or not. The router's header family is `x-llm-d-*`.
                continue
            if name in table.BACKEND_HEADERS:
                assert value in (new.backend_pod, new.extra.get(name)), (name, headers)
            else:
                assert new.extra.get(name) == value, (name, headers)
        if old_served.backend_pod is not None:
            recorded = {new.backend_pod, *new.extra.values()}
            assert old_served.backend_pod in recorded, headers


def test_a_backend_header_is_never_published():
    """`outcome.json` publishes the version signals; a backend value never reaches them. On a
    result, only values that name the serving endpoint are kept, never a Service or a URL."""
    pod_rows = {row.header for row in table.SIGNALS if row.pod}
    for headers, body in itertools.product(HEADER_SETS, BODIES):
        lowered = {k.lower(): v for k, v in (headers or {}).items()}
        assert not set(version_signals(lowered, body)) & table.BACKEND_HEADERS, headers
        extra = set(served_by(_exchange(headers, body)).extra)
        assert not (extra & table.BACKEND_HEADERS) - pod_rows, headers


def test_an_exact_backend_row_wins_over_a_published_prefix():
    headers = {"x-llm-d-destination-endpoint": "10.0.0.2:8000", "x-llm-d-epp": "epp-1"}
    assert version_signals(headers, None) == {"x-llm-d-epp": "epp-1"}
    assert served_by(_exchange(headers, None)).backend_pod == "10.0.0.2:8000"


def test_a_header_no_row_knows_is_not_recorded_unless_declared():
    headers = {"x-kong-version": "3.9.1", "x-forwarded-for": "10.1.1.1"}
    assert version_signals(headers, None) == {}
    assert served_by(_exchange(headers, None)).extra == {}
    declared = ("X-Kong-Version",)
    assert version_signals(headers, None, declared) == {"x-kong-version": "3.9.1"}
    assert served_by(_exchange(headers, None), declared).extra == {"x-kong-version": "3.9.1"}
    assert "x-kong-version" in table.build_signal_names(declared)


def test_only_build_rows_name_a_build():
    builds = table.build_signal_names()
    assert builds == {"system_fingerprint", "x-litellm-version"}
    # No prefix row names a build: a header family holds per-request values.
    assert not any(row.build for row in table.SIGNALS if row.prefix)


def test_a_body_marker_is_found_in_json_and_in_raw_text():
    marker = "inference error: BadRequest - no model"
    found = table.classify({}, {"error": {"message": marker}})
    assert [f.reveals for f in found] == [table.Reveals.SIGNATURE]
    found = table.classify({}, None, text=marker)
    assert [f.reveals for f in found] == [table.Reveals.SIGNATURE]


def test_a_profile_cannot_declare_a_backend_header_as_a_version_header(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text(
        "schema: llmdv.profile/1\nname: p\nmodel: {public_id: m}\ningresses:\n"
        "  - id: edge\n    kind: gateway\n    base_url: http://localhost:1\n"
        "    version_headers: [x-kong-version, X-Envoy-Upstream-Host]\n"
    )
    with pytest.raises(ProfileError, match=r"version_headers\[1\].*x-envoy-upstream-host"):
        loader.load(path)


def _declaring(tmp_path, header):
    path = tmp_path / "p.yaml"
    path.write_text(
        "schema: llmdv.profile/1\nname: p\nmodel: {public_id: m}\ningresses:\n"
        "  - id: edge\n    kind: gateway\n    base_url: http://localhost:1\n"
        f"    version_headers: ['{header}']\n"
    )
    return path


@pytest.mark.parametrize(
    "header",
    [s.header for s in table.SIGNALS if s.reveals is table.Reveals.BACKEND and s.header]
    + [
        f"{s.prefix}-variant"
        for s in table.SIGNALS
        if s.reveals is table.Reveals.BACKEND and s.prefix
    ],
)
def test_no_header_a_backend_row_matches_can_be_declared(tmp_path, header):
    """Exact or by prefix: a declared header is published in outcome.json, and any backend row
    may name a host, so none can be declared, including rows added later."""
    with pytest.raises(ProfileError, match="may name a host"):
        loader.load(_declaring(tmp_path, header))


def test_a_destination_variant_is_never_published_undeclared():
    """The GIE protocol's served variant names the endpoint as its parent does."""
    headers = {"x-gateway-destination-endpoint-served": "10.0.0.5:8000"}
    assert [f.reveals for f in table.classify(headers)] == [table.Reveals.BACKEND]
    assert version_signals(headers, None) == {}


def test_a_published_header_that_is_not_a_build_can_be_declared_one(tmp_path):
    """`server` is often the only build signal an unknown gateway sends (Kong: kong/3.4.0)."""
    assert loader.load(_declaring(tmp_path, "server")).ingress("edge").version_headers == (
        "server",
    )


def test_declared_version_headers_load_lower_cased(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text(
        "schema: llmdv.profile/1\nname: p\nmodel: {public_id: m}\ningresses:\n"
        "  - id: edge\n    kind: gateway\n    base_url: http://localhost:1\n"
        "    version_headers: [X-Kong-Version]\n"
    )
    assert loader.load(path).ingress("edge").version_headers == ("x-kong-version",)


def test_served_by_keeps_its_shape():
    assert served_by(_exchange(None, None)) == ServedBy()


def test_the_backend_chosen_does_not_depend_on_header_order():
    """Several backend headers in one response: the table's order decides, never the order the
    response happened to list them in."""
    pairs = [
        ("x-litellm-deployment", "deploy-a"),
        ("x-gateway-destination-endpoint", "10.0.0.1:8000"),
        ("x-envoy-upstream-host", "10.0.0.3:8000"),
    ]
    answers = {
        served_by(_exchange(dict(order), None)).to_dict()["backend_pod"]
        for order in itertools.permutations(pairs)
    }
    # The router's destination names the pod, the deepest answer; the gateway's id is kept too.
    assert answers == {"10.0.0.1:8000"}
    extras = {
        json.dumps(served_by(_exchange(dict(order), None)).to_dict())
        for order in itertools.permutations(pairs + [("server", "uvicorn"), ("x-llm-d-epp", "e")])
    }
    assert len(extras) == 1, "the recorded JSON is the same for any header order"


def test_a_declared_header_wins_over_a_recorded_prefix_but_never_over_a_backend_row():
    headers = {"x-envoy-build": "1.31", "x-envoy-upstream-host": "10.0.0.3:8000"}
    assert version_signals(headers, None, ("x-envoy-build",)) == {"x-envoy-build": "1.31"}
    assert version_signals(headers, None, ("x-envoy-upstream-host",)) == {}


def test_a_gateway_in_front_of_the_router_records_the_pod_and_its_deployment():
    """The common stack: LiteLLM's deployment id and the router's destination both reach the
    gateway's response. The pod is the deeper answer; the deployment id is kept too."""
    headers = {
        "x-litellm-deployment": "deploy-a",
        "x-litellm-model-api-base": "http://router.internal:80",
        "x-llm-d-destination-endpoint": "10.0.0.2:8000",
    }
    served = served_by(_exchange(headers, None))
    assert served.backend_pod == "10.0.0.2:8000"
    assert served.extra["x-litellm-deployment"] == "deploy-a"
    # An upstream URL names a Service, not the pod: kept off the record entirely.
    assert "http://router.internal:80" not in {served.backend_pod, *served.extra.values()}


def test_a_service_name_and_a_recorded_family_stay_off_the_record():
    headers = {
        "x-envoy-decorator-operation": "svc.ns.svc.cluster.local:80/*",
        "x-envoy-upstream-service-time": "12",
        "x-litellm-call-id": "call-1",
    }
    served = served_by(_exchange(headers, None))
    assert served.extra == {"x-litellm-call-id": "call-1"}
    assert served.backend_pod is None
    assert version_signals(headers, None) == {}


def test_the_whole_record_is_the_same_for_any_header_order():
    pairs = [
        ("x-litellm-deployment", "deploy-a"),
        ("x-gateway-destination-endpoint", "10.0.0.1:8000"),
        ("x-llm-d-epp", "e"),
        ("x-llm-d-router-version", "v1"),
        ("server", "uvicorn"),
        ("x-ratelimit-remaining-requests", "9"),
        ("retry-after", "3"),
        ("via", "1.1 proxy"),
        ("x-kong-version", "3.9"),
    ]
    shuffler = random.Random(7)
    records = set()
    for _ in range(200):
        order = pairs[:]
        shuffler.shuffle(order)
        served = served_by(_exchange(dict(order), None), ("x-kong-version",))
        records.add(json.dumps(served.to_dict()))
    assert len(records) == 1


@pytest.mark.parametrize(
    "declared, problem",
    [
        ("[x-litellm-version]", "already in the evidence table"),
        ("[x-litellm-call-id]", "already in the evidence table"),
        ("[x-gateway-destination-endpoint-served]", "may name a host"),
        ("[X-LLM-D-Destination-Endpoint-Pod]", "may name a host"),
        ("[x-kong-version, X-Kong-Version]", "declared twice"),
        ('["x-envoy-upstream-host "]', "may name a host"),
        ('[" X-Litellm-Model-Id"]', "may name a host"),
        ('[""]', "not a header name"),
        ('["x kong"]', "not a header name"),
        ('["x-kong:version"]', "not a header name"),
    ],
)
def test_a_profile_declares_only_headers_the_table_does_not_know(tmp_path, declared, problem):
    path = tmp_path / "p.yaml"
    path.write_text(
        "schema: llmdv.profile/1\nname: p\nmodel: {public_id: m}\ningresses:\n"
        "  - id: edge\n    kind: gateway\n    base_url: http://localhost:1\n"
        f"    version_headers: {declared}\n"
    )
    with pytest.raises(ProfileError, match=problem):
        loader.load(path)


def test_a_declared_header_can_still_claim_one_from_a_recorded_family():
    """`x-envoy-*` is recorded evidence by default; an operator who knows their proxy sends its
    build in one such header can say so."""
    found = table.classify({"x-envoy-build": "1.31"}, None, ("x-envoy-build",))
    assert [(f.signal, f.reveals, f.build) for f in found] == [
        ("x-envoy-build", table.Reveals.VERSION, True)
    ]


def test_a_padded_declared_name_is_trimmed_so_it_matches(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text(
        "schema: llmdv.profile/1\nname: p\nmodel: {public_id: m}\ningresses:\n"
        "  - id: edge\n    kind: gateway\n    base_url: http://localhost:1\n"
        '    version_headers: ["X-Kong-Version "]\n'
    )
    declared = loader.load(path).ingress("edge").version_headers
    assert version_signals({"x-kong-version": "1"}, None, declared) == {"x-kong-version": "1"}
