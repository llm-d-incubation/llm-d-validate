"""Model discovery: does the fleet advertise the model it is supposed to serve, and does it
refuse one it does not have?

Without the third case here, a stack that silently serves a *different* model than the one
requested passes every other test in the suite. No status code reveals it, the response is
well-formed, and the only symptom is that evaluation numbers quietly describe the wrong
checkpoint. That is the failure this file exists for.
"""

from __future__ import annotations

import pytest

from ...assertions import assert_client_error, assert_error_envelope, assert_no_generation
from ...evidence import journal
from ..conftest import nonexistent_model_name, throttle_aware

# Listing models does not depend on a surface, so those cases run once per ingress; the
# no-fallback case sends a generation request and runs once per surface.
pytestmark = pytest.mark.surface_generic


@pytest.mark.case_id("discovery.models_lists_declared_model")
def test_models_lists_declared_model(client, model, ingress):
    """GET /v1/models advertises the profile's model and every declared alias.

    Where the listing reports `max_model_len` for a declared name, it must equal the ingress's
    declared `model.max_model_len`. A gateway may omit the field, and an ingress may declare no
    limit, so either absence is recorded and not judged.
    """
    exchange = client.get("/v1/models")

    assert exchange.transport_error is None, f"transport failure: {exchange.transport_error}"
    assert exchange.status == 200, f"expected 200 from /v1/models, got {exchange.status}"

    body = exchange.response_body
    assert isinstance(body, dict), (
        f"/v1/models did not return a JSON object: {exchange.response_text!r}"
    )
    assert body.get("object") == "list", f"expected object='list', got {body.get('object')!r}"

    advertised = {entry.get("id") for entry in (body.get("data") or [])}
    journal.record("http_status", exchange.status, "200", ok=exchange.status == 200)
    journal.record(
        "advertised_models",
        sorted(n for n in advertised if n),
        f"contains {model.public_id}",
        ok=all(name in advertised for name in model.all_names),
    )
    assert advertised, "/v1/models returned an empty data array"

    for name in model.all_names:
        assert name in advertised, (
            f"ingress {ingress.id!r} does not advertise {name!r}; it offers: "
            f"{sorted(n for n in advertised if n)}"
        )

    declared = model.max_model_len
    lengths = {
        entry.get("id"): entry["max_model_len"]
        for entry in body.get("data") or []
        if entry.get("id") in model.all_names and "max_model_len" in entry
    }
    wrong = {} if declared is None else {n: v for n, v in lengths.items() if v != declared}
    journal.record(
        "advertised_max_model_len",
        lengths,
        f"{declared} wherever the listing reports it; not judged when the listing omits the "
        "field or the ingress declares no max_model_len",
        ok=not wrong,
    )
    assert not wrong, (
        f"ingress {ingress.id!r} advertises max_model_len {wrong}, but the profile declares "
        f"{declared}; clients that size prompts from the listing use the wrong limit"
    )


@pytest.mark.case_id("discovery.models_parses_with_openai_sdk")
def test_models_parses_with_pinned_openai_sdk(openai_client, model, ingress):
    """The listing deserialises through the pinned OpenAI SDK, not just as raw JSON.

    A response can be valid JSON and still fail the SDK's own model, which is what a real
    client would hit first.
    """
    with throttle_aware(ingress):
        page = openai_client.models.list()
    ids = {model.id for model in page.data}
    journal.record(
        "sdk_parsed_models",
        sorted(ids),
        "typed objects from the pinned SDK",
        ok=model.public_id in ids,
    )
    assert model.public_id in ids, (
        f"pinned OpenAI SDK parsed the listing from {ingress.id!r} but {model.public_id!r} "
        f"was not in it: {sorted(ids)}"
    )


@pytest.mark.case_id("discovery.unknown_model_no_fallback")
def test_unknown_model_rejected_without_fallback(client, surface, ingress):
    """An unknown model is refused, and is never quietly served by another model.

    Two assertions, and the second is the one that matters: the status says the stack noticed,
    and the absence of a generation says it did not substitute. A layer that falls back to its
    only configured deployment passes the first and fails the second.
    """
    bogus = nonexistent_model_name()
    exchange = client.post(surface.path, json_body=surface.minimal_body(bogus, max_tokens=8))
    journal.record("requested_model", bogus, "a model that cannot exist")

    # Ordered worst-finding-first. A layer that falls back returns 200 with a generation, and
    # "it served a generation for a model that does not exist" is a far more useful report than
    # "expected 404, got 200" -- so the fallback check runs before the status check.
    assert_no_generation(exchange, surface)
    assert_client_error(exchange, ingress, "unknown_model")
    assert_error_envelope(exchange, surface, ingress, "unknown_model")
