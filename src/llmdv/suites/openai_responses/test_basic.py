"""A stateless non-streaming generation through the Responses surface."""

from __future__ import annotations

import pytest

from ...assertions import assert_answer_only
from ...surfaces.base import DEFAULT_ANSWER
from ..conftest import budget_for, check_completion, check_json_response

pytestmark = pytest.mark.surface("responses")


@pytest.mark.case_id("openai_responses.basic_non_streaming")
def test_responses_basic(client, surface, model, ingress):
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model))
    exchange = client.post(surface.path, json_body=body)
    response = check_json_response(exchange, ingress)
    completion = surface.parse_completion(response)
    check_completion(completion, surface, model)
    assert_answer_only(completion, model, DEFAULT_ANSWER)
