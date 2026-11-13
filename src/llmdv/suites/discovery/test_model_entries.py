"""Model entries carry the fields a typed client requires, `GET /v1/models/{id}` answers for
the served model as declared and refuses an unknown one, and `GET /v1/model/info` reports the
declared capabilities."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest

from ...assertions import assert_client_error, assert_not_throttled, fail, preview
from ...evidence import journal
from ...http.client import Exchange
from ..conftest import nonexistent_model_name

pytestmark = pytest.mark.surface_generic


def _model_path(name: str) -> str:
    # The pinned OpenAI SDK percent-encodes the whole id, slashes included.
    return f"/v1/models/{quote(name, safe='')}"


def _error_envelope(exchange: Exchange) -> None:
    body = exchange.response_body
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    ok = isinstance(message, str) and bool(message)
    journal.record("error_envelope", type(error).__name__, "an error object with a message", ok=ok)
    if not ok:
        fail("error_envelope", preview(exchange), "an error object with a message")


def _entry_problems(entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return ["not an object"]
    problems = []
    if not (isinstance(entry.get("id"), str) and entry["id"]):
        problems.append("id")
    if entry.get("object") != "model":
        problems.append("object")
    if type(entry.get("created")) is not int:
        problems.append("created")
    if not isinstance(entry.get("owned_by"), str):
        problems.append("owned_by")
    return problems


def _same(observed: Any, expected: Any) -> bool:
    # Booleans compare by identity so a null, 0 or "true" never passes for a declared flag;
    # a list such as supported_endpoints is unordered.
    if isinstance(expected, bool) or isinstance(observed, bool):
        return observed is expected
    if isinstance(expected, tuple):
        return (
            isinstance(observed, list)
            and all(isinstance(item, str) for item in observed)
            and sorted(observed) == sorted(expected)
        )
    return observed == expected


@pytest.mark.case_id("discovery.model_entry_fields")
def test_model_entries_have_typed_fields(client, ingress):
    """Each entry has a string id, object `model`, an integer created and a string owned_by."""
    exchange = client.get("/v1/models")
    assert_not_throttled(exchange, ingress)
    journal.record("http_status", exchange.status, "200", ok=exchange.status == 200)
    assert exchange.status == 200, f"expected 200 from /v1/models, got {exchange.status}"
    body = exchange.response_body
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or not data:
        fail("model_entries", type(data).__name__, "a non-empty data array")
    problems = {str(i): _entry_problems(entry) for i, entry in enumerate(data)}
    problems = {key: value for key, value in problems.items() if value}
    journal.record(
        "model_entry_fields",
        problems,
        "id, object, created and owned_by on every entry",
        ok=not problems,
    )
    if problems:
        fail("model_entry_fields", problems, "id, object, created and owned_by on every entry")


@pytest.mark.case_id("discovery.model_retrieve")
def test_served_model_retrieval(client, model, ingress):
    """Declared true: the served model is returned. Declared false: a clean 4xx."""
    declared = ingress.capabilities.model_retrieval
    if declared is None:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare model_retrieval")
    exchange = client.get(_model_path(model.public_id))
    assert_not_throttled(exchange, ingress)
    if not declared:
        assert_client_error(exchange, ingress, "unknown_model")
        _error_envelope(exchange)
        return
    body = exchange.response_body
    observed = (exchange.status, body.get("id") if isinstance(body, dict) else None)
    expected = (200, model.public_id)
    ok = observed == expected and body.get("object") == "model"
    journal.record("retrieved_model", observed, expected, ok=ok)
    if not ok:
        fail("retrieved_model", observed, expected)


@pytest.mark.case_id("discovery.model_retrieve_unknown")
def test_unknown_model_retrieval_is_refused(client, ingress):
    """An unknown id is refused with a 4xx, never answered with a model or a 5xx."""
    exchange = client.get(_model_path(nonexistent_model_name()))
    body = exchange.response_body
    served = isinstance(body, dict) and body.get("object") == "model"
    journal.record("model_returned", served, "no model for an unknown id", ok=not served)
    if served:
        fail("model_returned", body.get("id"), "no model for an unknown id")
    assert_client_error(exchange, ingress, "unknown_model")
    _error_envelope(exchange)


@pytest.mark.case_id("discovery.model_info")
def test_model_info_matches_declaration(client, model, ingress):
    """`GET /v1/model/info` reports every declared `model_info` field with the declared value.

    A gateway fills these from its own configuration, not from the model server, so a
    self-hosted model reports null until each is declared there. Clients read them to decide
    which features to send. Every entry the gateway lists under the model's name is judged.
    """
    declared = ingress.capabilities.model_info
    if not declared:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare model_info")
    exchange = client.get("/v1/model/info")
    assert_not_throttled(exchange, ingress)
    journal.record("http_status", exchange.status, "200", ok=exchange.status == 200)
    assert exchange.status == 200, f"expected 200 from /v1/model/info, got {exchange.status}"
    body = exchange.response_body
    data = body.get("data") if isinstance(body, dict) else None
    entries = [
        entry
        for entry in (data if isinstance(data, list) else [])
        if isinstance(entry, dict) and entry.get("model_name") == model.public_id
    ]
    if not entries:
        listed = sorted(
            str(entry.get("model_name"))
            for entry in (data if isinstance(data, list) else [])
            if isinstance(entry, dict)
        )
        fail("model_info_entry", listed, f"an entry with model_name {model.public_id!r}")
    wrong: dict[str, list[Any]] = {}
    for entry in entries:
        info = entry.get("model_info")
        info = info if isinstance(info, dict) else {}
        for name, expected in declared.items():
            if not _same(info.get(name), expected):
                wrong.setdefault(name, []).append(info.get(name))
    expected_view = {name: list(v) if isinstance(v, tuple) else v for name, v in declared.items()}
    journal.record(
        "model_info",
        wrong,
        f"every declared field as declared: {expected_view}",
        ok=not wrong,
    )
    if wrong:
        fail(
            "model_info_fields",
            wrong,
            {name: expected_view[name] for name in wrong},
        )
