"""Check reported reasoning tokens on APIs that expose a native counter."""

import pytest

from ...assertions.reasoning import assert_reasoning_usage
from ..conftest import budget_for, check_basic_completion


@pytest.mark.surface_generic("reasoning_usage")
@pytest.mark.needs("reasoning", "reasoning_tokens")
@pytest.mark.case_id("field_options.reasoning_usage")
def test_reasoning_usage(client, surface, model, ingress):
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model))
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    assert_reasoning_usage(completion)
