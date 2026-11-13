"""Credentials in the shapes clients actually send them.

A gateway sees malformed Authorization headers from misconfigured clients, keys in x-api-key
from Anthropic SDKs and in api-key from Azure-style clients. An unauthenticated layer sees an
Authorization header from every SDK, which always sends one. None of these may produce a 5xx,
and alternate headers are accepted or refused exactly as the profile declares.
"""

from __future__ import annotations

import base64
import uuid

import pytest

from ...assertions import assert_client_error, assert_error_envelope
from ...evidence import journal
from ...profile.schema import AuthType
from ..transport.helpers import body, carrier, check_served

CREDENTIAL_HEADERS = ("Authorization", "x-api-key", "api-key")


def _others(sent: dict[str, str]) -> tuple[str, ...]:
    """The credential headers to drop so only the probe's own credential is sent."""
    names = {name.lower() for name in sent}
    return tuple(name for name in CREDENTIAL_HEADERS if name.lower() not in names)


def _bogus() -> str:
    return f"llmdv-invalid-{uuid.uuid4().hex[:12]}"


@pytest.mark.case_id("headers.malformed_credentials")
def test_malformed_credentials(client, model, ingress):
    """A Bearer without a token, a bare key and a Basic credential each get 401 and an envelope."""
    if ingress.auth.type is AuthType.NONE:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not authenticate")
    surface = carrier(ingress)
    basic = base64.b64encode(f"llmdv:{_bogus()}".encode()).decode()
    variants = {
        "bearer_without_token": {"Authorization": "Bearer"},
        "no_scheme": {"Authorization": _bogus()},
        "basic": {"Authorization": f"Basic {basic}"},
    }
    if ingress.auth.type is AuthType.API_KEY:
        variants = {"empty_key": {"x-api-key": ""}, **variants}
    for name, headers in variants.items():
        exchange = client.post(
            surface.path,
            json_body=body(surface, model),
            headers={**surface.extra_headers(), **headers},
            omit_headers=_others(headers),
        )
        journal.record("credential_shape", name)
        assert_client_error(exchange, ingress, "bad_credentials")
        assert_error_envelope(exchange, surface, ingress, "bad_credentials")


@pytest.mark.case_id("headers.alternate_credentials")
def test_alternate_credential_headers(request, client, model, ingress):
    """The bearer key in x-api-key or api-key is accepted where declared, and refused elsewhere."""
    declared = ingress.transport.credential_headers
    if ingress.auth.type is not AuthType.BEARER or declared is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no transport.credential_headers "
            "for a bearer credential"
        )
    secret = getattr(request.config, "_llmdv_secrets", {}).get(ingress.id)
    if not secret:
        pytest.skip(f"[undeclared] no credential resolved for ingress {ingress.id!r}")
    surface = carrier(ingress)
    for header in ("x-api-key", "api-key"):
        exchange = client.post(
            surface.path,
            json_body=body(surface, model),
            headers={**surface.extra_headers(), header: secret},
            omit_headers=_others({header: secret}),
        )
        accepted = header in declared
        journal.record("alternate_header", header, "accepted" if accepted else "refused")
        if accepted:
            check_served(exchange, surface, model, ingress)
        else:
            assert_client_error(exchange, ingress, "bad_credentials")


@pytest.mark.case_id("headers.credentials_ignored")
def test_unauthenticated_ingress_ignores_credentials(client, model, ingress):
    """An ingress without authentication serves a request that carries an Authorization header."""
    if ingress.auth.type is not AuthType.NONE:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} authenticates")
    surface = carrier(ingress)
    exchange = client.post(
        surface.path,
        json_body=body(surface, model),
        headers={**surface.extra_headers(), "Authorization": f"Bearer {_bogus()}"},
    )
    check_served(exchange, surface, model, ingress)
