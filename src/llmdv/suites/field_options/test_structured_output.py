"""A declared JSON Schema option must constrain the answer on every supported surface."""

from __future__ import annotations

import pytest

from ...assertions.structured_output import assert_structured_answer
from ..conftest import budget_for, check_basic_completion
from .probe import schema_probe


@pytest.mark.surface_generic("structured_output")
@pytest.mark.needs("structured_output")
@pytest.mark.case_id("field_options.structured_output")
def test_structured_output(client, surface, model, ingress):
    # The marker lives only in the schema: ignoring the option cannot pass by following
    # the prompt or returning a fixed JSON object. Use the common supported schema subset.
    schema = schema_probe()
    body = surface.structured_output_body(
        model.public_id,
        schema,
        max_tokens=budget_for(model, floor=256),
        prompt="Return a JSON object matching the supplied output schema. Set count to 1.",
    )
    exchange = client.post(surface.path, json_body=body)
    completion = check_basic_completion(exchange, surface, model, ingress)
    assert_structured_answer(completion, schema, model)
