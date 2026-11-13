"""Verify the profile against reality, in both directions.

A declaration nobody checks is a comment. These cases make the profile falsifiable:

    declared present, actually absent  -> FAIL, the surface regressed
    declared absent,  actually present -> FAIL, the profile is stale and coverage is
                                          silently missing
    declared, not observable from here -> SKIP, naming the instrument that is missing

The second row is the one teams lose. A fleet that gains `/v1/responses` while the ingress's
`surfaces` list still omits it keeps passing a green suite that never tests it, and nobody finds
out until a client depends on it.

These run before the functional suites, because a stale profile makes every result below it
harder to interpret.
"""

from __future__ import annotations

import uuid

import pytest

from ... import surfaces
from ...assertions import assert_not_throttled
from ...assertions.header_probe import assert_backend_header
from ...evidence import journal
from ...http.redact import SECRET_HEADERS
from ...profile.schema import Disposition, SkipClass
from ...surfaces.base import SURFACE_PATHS, probe_request
from ..conftest import check_json_response

pytestmark = pytest.mark.contract

# Only a successful valid request proves a surface is served. Gateways may answer
# unknown routes with 400, 401 or 405 before any engine route resolves.


@pytest.mark.case_id("contract.declared_surfaces_exist")
def test_declared_surfaces_exist(client, model, ingress):
    """Every declared surface serves a valid request successfully."""
    missing = []
    for name in ingress.surfaces:
        path = SURFACE_PATHS.get(name)
        if path is None:
            continue
        body, headers = probe_request(name, model.public_id)
        exchange = client.post(path, json_body=body, headers=headers)
        assert_not_throttled(exchange, ingress)
        if exchange.transport_error:
            missing.append(f"{name} ({path}): transport error {exchange.transport_error}")
        elif exchange.status is None or not 200 <= exchange.status < 300:
            missing.append(f"{name} ({path}): HTTP {exchange.status}, but the profile declares it")

    journal.record(
        "probed_declared",
        sorted(ingress.surfaces),
        "all serve valid requests with 2xx",
        ok=not missing,
    )
    assert not missing, (
        f"ingress {ingress.id!r} declares surfaces its fleet does not serve:\n  "
        + "\n  ".join(missing)
    )


@pytest.mark.case_id("contract.undeclared_surfaces_absent")
def test_undeclared_surfaces_are_really_absent(client, model, ingress):
    """A surface the profile does not declare is genuinely not there.

    This is the stale-profile direction. A surface that answers while undeclared is untested
    coverage, so it fails rather than passing quietly.
    """
    unexpected = []
    for name, path in sorted(SURFACE_PATHS.items()):
        if ingress.exposes(name):
            continue
        body, headers = probe_request(name, model.public_id)
        exchange = client.post(path, json_body=body, headers=headers)
        assert_not_throttled(exchange, ingress)
        if exchange.transport_error:
            continue
        if exchange.status is not None and 200 <= exchange.status < 300:
            unexpected.append(
                f"{name} ({path}): answered {exchange.status}, but the profile does not declare it"
            )

    journal.record(
        "probed_undeclared",
        sorted(n for n in SURFACE_PATHS if not ingress.exposes(n)),
        "none serve valid requests with 2xx",
        ok=not unexpected,
    )
    assert not unexpected, (
        f"ingress {ingress.id!r} serves surfaces the profile omits, so they are going untested:\n  "
        + "\n  ".join(unexpected)
        + f"\n\nAdd them to ingresses({ingress.id}).surfaces, or record why they are excluded."
    )


@pytest.mark.case_id("contract.header_disposition")
def test_header_disposition(client_factory, profile, declared_header):
    """Verify one declared header disposition, or skip naming the missing instrument.

    Only `status` and `response_headers` are observable from a client. Stripping, forwarding
    and rewriting happen between layers and need an instrumented backend; `consumed` needs a
    load experiment to show a priority band actually took effect. Those skip with the class
    recorded, which is the honest outcome -- passing them would assert something the evidence
    does not support.
    """
    header = declared_header.header
    ingress_id = declared_header.ingress_id
    disposition = declared_header.disposition

    if not disposition.client_observable:
        skip_class = SkipClass.for_disposition(disposition)
        pytest.skip(
            f"[{skip_class.value}] {header!r} is declared {disposition.value!r} at "
            f"{ingress_id!r}, which is only verifiable by {disposition.verifiable_by.value}"
        )

    # Sent through a surface this ingress actually exposes. A fixed surface would 404 on an
    # ingress without it, and an `ignored` header would then fail as if it had broken the
    # request.
    exposed = profile.ingress(ingress_id).surfaces
    carrier = next((name for name in exposed if name in surfaces.implemented()), None)
    if carrier is None:
        skip_class = SkipClass.NOT_IMPLEMENTED if exposed else SkipClass.UNDECLARED
        pytest.skip(
            f"[{skip_class.value}] {ingress_id!r} exposes no surface llmdv can drive, so "
            f"there is no request to carry {header!r}"
        )

    surface = surfaces.get(carrier)
    client = client_factory(ingress_id)
    model = profile.ingress(ingress_id).model
    probe_value = f"llmdv-probe-{header}"
    body = surface.minimal_body(model.public_id, max_tokens=8)
    sent = surface.extra_headers()
    if disposition not in (Disposition.PROMISED, Disposition.ABSENT):
        sent = {**sent, header: probe_value}
    exchange = client.post(surface.path, json_body=body, headers=sent)
    # A throttled request proves nothing about a header's disposition: the gateway refused us
    # before the header could have an effect either way.
    assert_not_throttled(exchange, profile.ingress(ingress_id))
    returned = {name.lower(): value for name, value in (exchange.response_headers or {}).items()}
    journal.record("carried_by", carrier)
    journal.record(f"sent {header}", probe_value, f"disposition {disposition.value}")
    journal.record("status", exchange.status)
    journal.record("returned_header", returned.get(header.lower()))

    if disposition in (Disposition.PROMISED, Disposition.ABSENT):
        assert exchange.status == 200, (
            f"response-header contract requires HTTP 200, got {exchange.status}"
        )
    if disposition is Disposition.REJECTED:
        assert exchange.status is not None and 400 <= exchange.status < 500, (
            f"{header!r} is declared 'rejected' at {ingress_id!r} but the request succeeded "
            f"with {exchange.status}"
        )
    elif disposition is Disposition.ECHOED:
        assert returned.get(header.lower()) == probe_value, (
            f"{header!r} is declared 'echoed' at {ingress_id!r} but the response did not return "
            f"it (got {returned.get(header.lower())!r})"
        )
    elif disposition is Disposition.IGNORED:
        assert exchange.status == 200, (
            f"{header!r} is declared 'ignored' at {ingress_id!r} but sending it changed the "
            f"outcome: status {exchange.status}, body "
            f"{(exchange.response_body or exchange.response_text)!r}"
        )
    elif disposition is Disposition.ABSENT:
        assert header.lower() not in returned, (
            f"{header!r} is declared 'absent' at {ingress_id!r} but the response included it"
        )
    elif disposition is Disposition.PROMISED:
        assert returned.get(header.lower(), "").strip(), (
            f"{header!r} is declared 'promised' at {ingress_id!r} but the response omitted it"
        )


@pytest.mark.case_id("contract.header_backend_disposition")
def test_header_backend_disposition(client_factory, profile, declared_header):
    header, ingress_id, disposition = (
        declared_header.header,
        declared_header.ingress_id,
        declared_header.disposition,
    )
    target = profile.ingress(ingress_id)
    if not target.capabilities.header_probe:
        pytest.skip(f"[undeclared] ingress {ingress_id!r} declares no header_probe")
    if disposition not in (Disposition.FORWARDED, Disposition.STRIPPED, Disposition.REWRITTEN):
        pytest.skip("[undeclared] this disposition is not a backend header contract")
    if header.lower() in SECRET_HEADERS:
        pytest.skip("[requires_instrumentation] the echo backend never reports credential headers")
    if not target.exposes("chat_completions"):
        pytest.skip("[not_implemented] header echo instrument exposes Chat only")
    rewrite = target.capabilities.header_rewrites.get(header)
    if disposition is Disposition.REWRITTEN and rewrite is None:
        pytest.skip(f"[undeclared] no expected header_rewrites value for {header!r}")
    surface = surfaces.get("chat_completions")
    client = client_factory(ingress_id)
    probe_id, probe_value = uuid.uuid4().hex, f"llmdv-probe-{header}"
    exchange = client.post(
        surface.path,
        json_body=surface.minimal_body(target.model.public_id, max_tokens=8),
        headers={header: probe_value, "x-llmdv-probe-id": probe_id},
    )
    assert_backend_header(
        check_json_response(exchange, target), header, disposition, probe_value, probe_id, rewrite
    )
