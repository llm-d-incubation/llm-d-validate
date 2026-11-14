"""Response ids a fleet never stored must be refused, never answered without their context.

Every id here is fresh or was created by the case itself. No case deletes or cancels anything.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import quote

import pytest

from ...assertions import (
    assert_answer_only,
    assert_client_error,
    assert_error_envelope,
    assert_has_answer,
    fail,
)
from ...evidence import journal
from ...http.client import Exchange
from ...profile.schema import Ingress, Model
from ...surfaces.base import Surface
from ..conftest import budget_for, check_completion, check_json_response

pytestmark = pytest.mark.surface("responses")

CODE_WORD = "basalt"
REMEMBER = f"The code word is {CODE_WORD}. Reply with the single word: ok"
QUESTION = "What is the code word? Reply with the code word only."


def fresh_response_id() -> str:
    """An id no fleet can have stored: random per call."""
    return f"resp_llmdv{uuid.uuid4().hex}"


def response_path(surface: Surface, identity: str, suffix: str = "") -> str:
    return f"{surface.path}/{quote(identity, safe='')}{suffix}"


def check_refused(exchange: Exchange, surface: Surface, ingress: Ingress, what: str) -> None:
    """A clean refusal: no generation, a 4xx and a parseable error envelope."""
    served = surface.looks_like_success(exchange.response_body)
    journal.record(f"{what}_generation", served, "no generation", ok=not served)
    if served:
        fail(f"{what}_generation", "a generation", "a refusal, never an answer without context")
    assert_client_error(exchange, ingress, "unknown_response")
    assert_error_envelope(exchange, surface, ingress, "unknown_response")


def create(client, surface: Surface, model: Model, ingress: Ingress, **extra: Any) -> dict:
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model), **extra)
    created = check_json_response(client.post(surface.path, json_body=body), ingress)
    check_completion(surface.parse_completion(created), surface, model)
    identity = created.get("id")
    ok = isinstance(identity, str) and bool(identity)
    journal.record("response_identity", identity, "a non-empty response id", ok=ok)
    if not ok:
        fail("response_identity", identity, "a non-empty response id")
    return created


def chain(client, surface: Surface, model: Model, identity: str) -> Exchange:
    body = surface.minimal_body(
        model.public_id,
        max_tokens=budget_for(model),
        prompt=QUESTION,
        previous_response_id=identity,
    )
    return client.post(surface.path, json_body=body)


@pytest.mark.case_id("openai_responses.previous_id_unknown")
def test_unknown_previous_response_id_is_refused(client, surface, model, ingress):
    """Chaining from an id nobody stored must fail; a generation would have lost the context."""
    identity = fresh_response_id()
    journal.record("previous_response_id", identity, "an id no fleet can have stored")
    check_refused(chain(client, surface, model, identity), surface, ingress, "unknown_previous")


@pytest.mark.case_id("openai_responses.previous_id_unstored")
def test_unstored_previous_response_id_is_refused(client, surface, model, ingress):
    """A response created with store false can never be chained, on any fleet."""
    identity = create(client, surface, model, ingress, store=False)["id"]
    check_refused(chain(client, surface, model, identity), surface, ingress, "unstored_previous")


@pytest.mark.case_id("openai_responses.retrieve_unknown")
@pytest.mark.parametrize("suffix", ["", "/input_items"], ids=["response", "input_items"])
def test_unknown_response_retrieval_is_refused(client, surface, ingress, suffix):
    """Read-only lookups of an id nobody stored answer a clean 4xx, never 200 or 5xx."""
    exchange = client.get(response_path(surface, fresh_response_id(), suffix))
    assert_client_error(exchange, ingress, "unknown_response")
    assert_error_envelope(exchange, surface, ingress, "unknown_response")


@pytest.mark.needs("stateless_responses")
@pytest.mark.case_id("openai_responses.stateless_contract")
@pytest.mark.parametrize("operation", ["retrieval", "chaining", "background"])
def test_stateless_fleet_refuses_stored_state(client, surface, model, ingress, operation):
    """A fleet declared to keep no response state still generates for store true, the API
    default, and refuses every operation that needs the stored response."""
    if operation == "background":
        body = surface.minimal_body(
            model.public_id, max_tokens=budget_for(model), store=True, background=True
        )
        check_refused(client.post(surface.path, json_body=body), surface, ingress, "background")
        return
    identity = create(client, surface, model, ingress, store=True)["id"]
    if operation == "retrieval":
        exchange = client.get(response_path(surface, identity))
        assert_client_error(exchange, ingress, "unknown_response")
        assert_error_envelope(exchange, surface, ingress, "unknown_response")
    else:
        check_refused(chain(client, surface, model, identity), surface, ingress, "stored_previous")


@pytest.mark.needs("stateful_responses")
@pytest.mark.case_id("openai_responses.previous_id_chain")
def test_previous_response_id_carries_context(client, surface, model, ingress):
    """A stored turn chained by id gives the next turn its context: the code word."""
    budget = budget_for(model)
    identity = create(client, surface, model, ingress, store=True, prompt=REMEMBER)["id"]
    exchange = chain(client, surface, model, identity)
    completion = surface.parse_completion(check_json_response(exchange, ingress))
    check_completion(completion, surface, model)
    assert_has_answer(completion, budget)
    assert_answer_only(completion, model, CODE_WORD)
