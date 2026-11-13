"""Assembled streaming output must obey the exact supplied schema."""

import pytest

from ...assertions.structured_output import assert_structured_answer
from ..conftest import budget_for, check_basic_completion
from ..streaming import check_stream_completion
from .probe import schema_probe


@pytest.mark.surface_generic("structured_output")
@pytest.mark.needs("structured_output", "structured_output_streaming")
@pytest.mark.case_id("field_options.structured_output_stream")
def test_streamed_schema(client, surface, model, ingress):
    schema = schema_probe()
    body = surface.structured_output_body(
        model.public_id,
        schema,
        budget_for(model, floor=256),
        "Return a JSON object matching the supplied schema.",
    )
    body = {**surface.stream_body(model.public_id, include_usage=True), **body}
    completion = check_stream_completion(client, surface, model, ingress, body)
    assert_structured_answer(completion, schema, model)


@pytest.mark.surface_generic("json_object")
@pytest.mark.needs("json_object")
@pytest.mark.case_id("field_options.json_object")
def test_json_object(client, surface, model, ingress):
    body = surface.json_object_body(model.public_id, budget_for(model, floor=256))
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    assert_structured_answer(completion, {"type": "object"}, model)
