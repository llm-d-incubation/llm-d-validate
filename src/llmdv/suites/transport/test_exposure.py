"""What an ingress exposes beyond its APIs, checked against what the profile declares.

Every probe is a GET to a path the profile names. Engine admin and dev-mode routes cannot be
declared (the loader refuses them), so no case here can reach one, read-only or not. A path
declared absent must answer one of `transport.absent_status`, quickly; a path declared present
must answer 2xx. A public schema or version string tells anyone which engine routes and which
release sit behind the edge.
"""

from __future__ import annotations

import pytest

from ...assertions.transport import assert_answered, assert_fast
from ...evidence import journal
from ...profile.schema import Disposition

pytestmark = pytest.mark.contract


@pytest.mark.case_id("transport.health_status")
def test_health_status(client, ingress):
    """GET health_path answers the declared status, quickly."""
    declared = ingress.transport.health_status
    if not declared:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no transport.health_status")
    exchange = client.get(ingress.health_path)
    ok = exchange.transport_error is None and exchange.status in declared
    journal.record("health_status", exchange.status, f"one of {list(declared)}", ok=ok)
    assert ok, f"GET {ingress.health_path} answered {exchange.status} {exchange.transport_error}"
    assert_fast(exchange, "health")


@pytest.mark.case_id("transport.present_paths")
def test_declared_present_paths(client, ingress):
    """Each declared present path answers a GET with 2xx."""
    paths = ingress.transport.present_paths
    if not paths:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no transport.present_paths")
    missing = []
    for path in paths:
        exchange = client.get(path)
        journal.record(f"GET {path}", exchange.status or exchange.transport_error)
        if exchange.status is None or not 200 <= exchange.status < 300:
            missing.append(f"{path}: {exchange.status or exchange.transport_error}")
    journal.record("present_paths", list(paths), "each answers 2xx", ok=not missing)
    assert not missing, "declared present paths did not answer 2xx: " + "; ".join(missing)


@pytest.mark.case_id("transport.absent_paths")
def test_declared_absent_paths(client, ingress):
    """Each declared absent path refuses a GET with a declared status, quickly.

    A POST-only engine route that is reachable answers a GET with 405, which is how a GET can
    show `/tokenize` is exposed without sending it any work.
    """
    contract = ingress.transport
    if not contract.absent_paths:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no transport.absent_paths")
    exposed = []
    for path in contract.absent_paths:
        exchange = client.get(path)
        journal.record(f"GET {path}", exchange.status or exchange.transport_error)
        assert_answered(exchange, path)
        assert_fast(exchange, path)
        if exchange.status not in contract.absent_status:
            exposed.append(f"{path}: {exchange.status}")
    journal.record(
        "absent_paths",
        list(contract.absent_paths),
        f"each answers one of {list(contract.absent_status)}",
        ok=not exposed,
    )
    assert not exposed, "paths declared absent are reachable: " + "; ".join(exposed)


@pytest.mark.case_id("transport.bodiless_header")
def test_ignored_headers_on_a_bodiless_request(client, profile, ingress):
    """A header declared ignored is ignored on GET /v1/models too, which carries no body.

    A layer that routes bodiless requests on a different path can honor a header there that it
    overwrites on generation requests. The value names a closed local port, so honoring it
    fails the request.
    """
    ignored = sorted(
        header
        for header, per_ingress in profile.headers.items()
        if per_ingress.get(ingress.id) is Disposition.IGNORED
    )
    if not ignored:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no ignored header")
    exchange = client.get("/v1/models", headers={name: "127.0.0.1:1" for name in ignored})
    journal.record("sent_ignored_headers", ignored)
    ok = exchange.status == 200
    journal.record("models_status", exchange.status or exchange.transport_error, "200", ok=ok)
    assert ok, (
        f"GET /v1/models with declared-ignored headers {ignored} answered "
        f"{exchange.status or exchange.transport_error}"
    )
    names = [item.get("id") for item in (exchange.response_body or {}).get("data") or []]
    listed = ingress.model.public_id in names
    journal.record("model_listed", names, ingress.model.public_id, ok=listed)
    assert listed, f"GET /v1/models did not list {ingress.model.public_id!r}: {names}"
