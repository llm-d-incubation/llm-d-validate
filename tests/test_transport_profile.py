"""Loading an ingress's `transport` declarations."""

from __future__ import annotations

import pytest

from llmdv.profile import loader
from llmdv.profile.loader import ProfileError
from llmdv.profile.transport import TransportContract, plain_http_url

BASE = """
schema: llmdv.profile/1
name: transport
model: {public_id: example/model}
ingresses:
  - id: edge
    kind: llmd_router
    base_url: https://edge.example
"""


def load(tmp_path, transport: str):
    path = tmp_path / "profile.yaml"
    path.write_text(BASE + ("    transport:\n" + transport if transport else ""))
    return loader.load(path).ingress("edge").transport


def test_an_ingress_without_transport_keeps_every_default(tmp_path):
    assert load(tmp_path, "") == TransportContract()
    assert TransportContract().credential_headers is None
    assert TransportContract().absent_status == (403, 404)


def test_declarations_are_parsed(tmp_path):
    contract = load(
        tmp_path,
        "      health_status: [200]\n"
        "      present_paths: [/version]\n"
        "      absent_paths: [/openapi.json, /docs]\n"
        "      http_url: http://edge.example\n"
        "      cors_preflight: refused\n"
        "      alpn: h2\n"
        "      credential_headers: []\n"
        "      idempotency: ignored\n"
        "      stream_budget: {first_event_s: 30, max_gap_s: 5, min_tokens_per_s: 20}\n"
        "      max_body_bytes: 1048576\n",
    )
    assert contract.max_body_bytes == 1048576
    assert contract.absent_paths == ("/openapi.json", "/docs")
    assert contract.credential_headers == ()
    assert contract.stream_budget.max_gap_s == 5.0
    assert contract.stream_budget.next_request_s is None


@pytest.mark.parametrize(
    "transport,message",
    [
        ("      absent_paths: [/is_sleeping]\n", "admin route"),
        ("      present_paths: [/collective_rpc]\n", "admin route"),
        ("      absent_paths: [openapi.json]\n", "starting with /"),
        ("      http_url: https://edge.example\n", "http://"),
        ("      alpn: h3\n", "expected one of"),
        ("      credential_headers: [authorization]\n", "expected one of"),
        ("      model_rewrite_header: x-api-key\n", "non-credential"),
        ("      stream_budget: {max_gap_s: -1}\n", "positive number"),
        ("      stream_budget: {gap: 1}\n", "unknown field"),
        ("      absent_status: [99]\n", "HTTP status"),
        ("      max_body_bytes: 0\n", "positive integer"),
        ("      max_body_bytes: 1.5\n", "positive integer"),
        ("      nonsense: true\n", "unknown field"),
    ],
)
def test_invalid_declarations_name_the_field(tmp_path, transport, message):
    with pytest.raises(ProfileError, match=message):
        load(tmp_path, transport)


def load_at(tmp_path, base_url_line: str, transport: str):
    path = tmp_path / "profile.yaml"
    path.write_text(
        BASE.replace("    base_url: https://edge.example\n", base_url_line + "\n")
        + "    transport:\n"
        + transport
    )
    return loader.load(path).ingress("edge")


@pytest.mark.parametrize("base_url", ["https://edge.example", "https://edge.example:443/v1"])
def test_http_redirect_expected_is_parsed(tmp_path, base_url):
    ingress = load_at(tmp_path, f"    base_url: {base_url}", "      http_redirect: expected\n")
    assert ingress.transport.http_redirect == "expected"
    assert ingress.transport.http_url is None


@pytest.mark.parametrize(
    "transport,message",
    [
        ("      http_url: http://edge.example\n      http_redirect: expected\n", "not both"),
        ("      http_redirect: true\n", "expected one of"),
        ("      http_redirect: required\n", "expected one of"),
    ],
)
def test_invalid_http_redirect_declarations(tmp_path, transport, message):
    with pytest.raises(ProfileError, match=message):
        load(tmp_path, transport)


@pytest.mark.parametrize(
    "base_url,message",
    [
        # An internal layer reached over plain HTTP, such as a port-forward to an engine.
        ("http://127.0.0.1:8000", "needs an https base URL"),
        ("http://edge.example", "needs an https base URL"),
        ("https://edge.example:8443", "names port 8443; declare transport.http_url"),
    ],
)
def test_http_redirect_on_an_unprobeable_literal_base_url_is_a_profile_error(
    tmp_path, base_url, message
):
    with pytest.raises(ProfileError, match=message):
        load_at(tmp_path, f"    base_url: {base_url}", "      http_redirect: expected\n")


def test_http_redirect_with_base_url_env_is_left_to_preflight(tmp_path, monkeypatch):
    """The variable's value is the operator's environment, so loading still succeeds."""
    monkeypatch.setenv("LLMDV_EDGE_URL", "http://edge.example")
    ingress = load_at(
        tmp_path, "    base_url_env: LLMDV_EDGE_URL", "      http_redirect: expected\n"
    )
    assert ingress.transport.http_redirect == "expected"


@pytest.mark.parametrize(
    "base_url,plain",
    [
        ("https://edge.example", "http://edge.example"),
        ("https://edge.example/", "http://edge.example"),
        ("https://Edge.Example:443/v1", "http://edge.example"),
        ("HTTPS://edge.example", "http://edge.example"),
        ("https://user@edge.example", "http://edge.example"),
        ("https://[::1]", "http://[::1]"),
    ],
)
def test_the_plain_address_is_the_base_url_host_over_http(base_url, plain):
    assert plain_http_url(base_url) == (plain, None)


@pytest.mark.parametrize(
    "base_url",
    ["http://edge.example", "https://edge.example:8443", "https://edge.example:99999", "", "x"],
)
def test_no_plain_address_without_an_https_base_url_on_the_default_port(base_url):
    url, reason = plain_http_url(base_url)
    assert url is None and reason.startswith("transport.http_redirect")
    # The reason is shown to the operator; it names no host.
    assert "edge.example" not in reason
