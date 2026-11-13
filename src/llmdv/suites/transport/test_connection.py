"""The connection a client gets: reuse, TLS version, ALPN, and the plain-http entry point."""

from __future__ import annotations

import socket
import ssl
from urllib.parse import urlsplit

import pytest

from ...assertions.transport import assert_fast
from ...evidence import journal
from ...http.client import RecordingClient
from ...profile.schema import ProxyMode
from ...profile.transport import plain_http_url

MODERN_TLS = ("TLSv1.2", "TLSv1.3")
REDIRECTS = (301, 302, 307, 308)


@pytest.mark.case_id("transport.connection")
def test_connection_reuse_and_tls(client, ingress):
    """Two sequential requests share a connection, and HTTPS negotiates TLS 1.2 or newer.

    A layer that closes every connection makes each SDK request pay a new handshake. The
    observed HTTP version, TLS version and ALPN protocol are recorded either way.
    """
    first = client.get("/v1/models", inspect_connection=True)
    second = client.get("/v1/models", inspect_connection=True)
    for name, exchange in (("first", first), ("second", second)):
        ok = exchange.transport_error is None and exchange.status == 200
        journal.record(f"{name}_status", exchange.status or exchange.transport_error, "200", ok=ok)
        assert ok, f"GET /v1/models failed: {exchange.status} {exchange.transport_error}"
    journal.record("connection", {"first": first.connection, "second": second.connection})
    closed = (first.response_headers.get("connection") or "").lower() == "close"
    local = (first.connection or {}).get("local_addr"), (second.connection or {}).get("local_addr")
    reused = local[0] is not None and local[0] == local[1]
    journal.record("connection_reused", reused, "reused unless the server closed it", ok=reused)
    assert reused, "the second request opened a new connection" + (
        " because the server sent Connection: close" if closed else ""
    )
    if ingress.base_url.startswith("https://"):
        version = (first.connection or {}).get("tls_version")
        ok = version in MODERN_TLS
        journal.record("tls_version", version, f"one of {list(MODERN_TLS)}", ok=ok)
        assert ok, f"negotiated {version!r}, below TLS 1.2"


def negotiate_alpn(url: str, offers: list[str], verify: bool, timeout: float) -> str | None:
    """Complete a TLS handshake offering `offers` and return the protocol the server chose."""
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or 443
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(offers)
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return tls.selected_alpn_protocol()


@pytest.mark.case_id("transport.alpn")
def test_alpn_selection(ingress):
    """Offered h2 and http/1.1, the ingress selects the declared protocol.

    Only the TLS handshake is performed; no HTTP request is sent. A change that adds or drops
    HTTP/2 at the edge changes multiplexing and proxy timeouts for every client.
    """
    declared = ingress.transport.alpn
    if declared is None:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no transport.alpn")
    if not ingress.base_url.startswith("https://"):
        pytest.fail(f"transport.alpn is declared but {ingress.id!r} is not an https ingress")
    if ingress.setup.proxy is ProxyMode.ALLOW:
        pytest.skip("[not_implemented] the ALPN probe opens a direct socket and cannot use a proxy")
    selected = negotiate_alpn(
        ingress.base_url, ["h2", "http/1.1"], ingress.tls.verify, ingress.timeouts.connect
    )
    journal.record("alpn_selected", selected, declared, ok=selected == declared)
    assert selected == declared, f"offered h2 and http/1.1, the ingress selected {selected!r}"


@pytest.mark.case_id("transport.http_redirect")
def test_plain_http_redirects_to_https(request, ingress):
    """The ingress's plain-http address answers a redirect to https, quickly.

    `transport.http_redirect: expected` probes `http://` plus the host of the ingress's own https
    base URL; `transport.http_url` names a plain listener elsewhere. The derived address is a
    hostname, so no message here quotes it; the exchange records it, as it records base_url.
    """
    url = ingress.transport.http_url
    shown = url
    if url is None and ingress.transport.http_redirect:
        url, reason = plain_http_url(ingress.base_url)
        if reason:
            # The loader and preflight refuse this first; it lands here only when preflight was
            # skipped, and a declared expectation that cannot be probed must not read as a skip.
            journal.record(
                "http_redirect_base_url",
                "not an https URL on the default port",
                "an https base URL on the default port",
                ok=False,
            )
            pytest.fail(reason)
        shown = "http://<ingress host>"
    if url is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares neither transport.http_redirect "
            "nor transport.http_url"
        )
    from ...plugin import _correlation_headers, register_client

    plain = RecordingClient(
        url,
        default_headers=_correlation_headers(request.config, request.node),
        connect_timeout=ingress.timeouts.connect,
        request_timeout=30.0,
        trust_env=ingress.setup.proxy is ProxyMode.ALLOW,
    )
    register_client(request.node, ingress, plain)
    with plain:
        exchange = plain.get(ingress.health_path)
    location = exchange.response_headers.get("location") or ""
    ok = exchange.status in REDIRECTS and location.startswith("https://")
    if shown != url:
        # A derived address: the Location names the gateway's host too, and the recorded check
        # reaches results.jsonl and the console, so only its scheme and path are kept there.
        location = _without_host(location)
    journal.record(
        "http_redirect",
        {"status": exchange.status or exchange.transport_error, "location": location},
        f"one of {list(REDIRECTS)} to https://",
        ok=ok,
    )
    assert ok, (
        f"GET {shown}{ingress.health_path} answered {exchange.status or exchange.transport_error} "
        f"with Location {location!r}; expected a redirect to https"
    )
    assert_fast(exchange, "http_redirect")


def _without_host(location: str) -> str:
    """`location` with any host replaced, so a check can show where it points but not the host."""
    try:
        parts = urlsplit(location)
    except ValueError:
        return "<unparseable Location>"
    if not parts.netloc:
        return location
    return parts._replace(netloc="<host>").geturl()
