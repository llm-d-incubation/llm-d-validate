"""A forced invocation must survive a complete tool-result round trip."""

import pytest

from ..conftest import budget_for, check_json_response
from .helpers import check_tool_completion, check_tool_result, tool_schema


@pytest.mark.surface_generic("tool_loop")
@pytest.mark.needs("tools")
@pytest.mark.case_id("tools.forced_loop")
def test_forced_tool_loop(client, surface, model, ingress):
    schema = tool_schema()
    body = surface.tool_body(model.public_id, schema, budget_for(model, floor=256))
    response = check_json_response(client.post(surface.path, json_body=body), ingress)
    completion = surface.parse_completion(response)
    check_tool_completion(completion, surface, model, schema)
    check_tool_result(client, surface, model, ingress, body, completion)
