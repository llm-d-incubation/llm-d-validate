"""Only responses created by this case are retrieved or cancelled."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest

from ...assertions import fail
from ...evidence import journal
from ...profile.schema import Model
from ...surfaces.base import Surface
from ..conftest import budget_for, check_completion, check_identity, check_json_response

pytestmark = [pytest.mark.surface("responses"), pytest.mark.needs("stateful_responses")]


def _identity(body: dict[str, Any], expected: Any = None) -> str:
    identity = body.get("id")
    if not (
        isinstance(identity, str) and bool(identity) and (expected is None or identity == expected)
    ):
        fail("response_identity", identity, expected or "a non-empty response ID")
    journal.record("response_identity", identity, expected or "a non-empty response ID", ok=True)
    return identity


def _completed(body: dict[str, Any], surface: Surface, model: Model) -> None:
    check_completion(surface.parse_completion(body), surface, model)


@pytest.mark.case_id("openai_responses.stored_retrieval")
def test_stored_response_retrieval(client, surface, model, ingress):
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model), store=True)
    created = check_json_response(client.post(surface.path, json_body=body), ingress)
    identity = _identity(created)
    _completed(created, surface, model)
    retrieved = check_json_response(
        client.get(f"{surface.path}/{quote(identity, safe='')}"), ingress
    )
    _identity(retrieved, identity)
    _completed(retrieved, surface, model)
    for name in ("status", "output", "usage"):
        if not (retrieved.get(name) == created.get(name)):
            fail(f"stored_response_{name}", retrieved.get(name), created.get(name))
        journal.record(f"stored_response_{name}", retrieved.get(name), created.get(name), ok=True)


@pytest.mark.needs("stateful_responses", "background_responses")
@pytest.mark.disruptive("creates and cancels a bounded background generation")
@pytest.mark.case_id("openai_responses.background_cancellation")
def test_background_response_cancellation(client, surface, model, ingress):
    body = surface.minimal_body(
        model.public_id,
        max_tokens=budget_for(model, floor=128),
        prompt="Count from 1 to 100, one number per line.",
        store=True,
        background=True,
    )
    created = check_json_response(client.post(surface.path, json_body=body), ingress)
    identity = _identity(created)
    check_identity(surface.parse_completion(created), surface, model)
    path = f"{surface.path}/{quote(identity, safe='')}"
    if created.get("status") in ("completed", "incomplete"):
        _completed(created, surface, model)
        pytest.skip(
            "[requires_load] response completed before cancellation; needs controlled pending work"
        )
    if not (created.get("status") in ("queued", "in_progress") and created.get("error") is None):
        fail("background_pending", created.get("status"), "queued or in_progress")
    journal.record("background_pending", created.get("status"), "queued or in_progress", ok=True)
    cancelled = client.post(f"{path}/cancel")
    # A bounded request can finish while the cancellation request is in flight.
    if cancelled.status == 400:
        latest = check_json_response(client.get(path), ingress)
        _identity(latest, identity)
        if latest.get("status") in ("completed", "incomplete"):
            _completed(latest, surface, model)
            pytest.skip(
                "[requires_load] response completed during cancellation; no cancellation proven"
            )
    result = check_json_response(cancelled, ingress)
    _identity(result, identity)
    check_identity(surface.parse_completion(result), surface, model)
    if not (result.get("status") == "cancelled"):
        fail("response_cancelled", result.get("status"), "cancelled")
    journal.record("response_cancelled", result.get("status"), "cancelled", ok=True)
    retrieved = check_json_response(client.get(path), ingress)
    _identity(retrieved, identity)
    check_identity(surface.parse_completion(retrieved), surface, model)
    if not (retrieved.get("status") == "cancelled"):
        fail("cancellation_persisted", retrieved.get("status"), "cancelled")
    journal.record("cancellation_persisted", retrieved.get("status"), "cancelled", ok=True)
