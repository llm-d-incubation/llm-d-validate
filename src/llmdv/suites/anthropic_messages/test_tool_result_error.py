"""A tool_result marked is_error tells the model the tool failed.

The flag is often the only signal: the content of a failed call can be a bare code such as
EACCES. A layer that drops the flag hands the model a failure that reads as ordinary output, so
an agent carries on as if the call worked. The flag must change the rendered prompt, so the
request that sets it reports more input tokens than the same request without it.
"""

import pytest

from ...assertions.context import assert_prompt_grows, prompt_size
from ...surfaces.base import TOOL_NAME, ToolSpec
from ..conftest import budget_for, check_call_markup, check_identity, check_json_response

pytestmark = [
    pytest.mark.surface("messages"),
    pytest.mark.needs("messages_checks:tool_result_error"),
]

SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
CALL_ID = "toolu_llmdv01"


def _body(surface, model, is_error: bool):
    body = surface.tools_body(
        model.public_id,
        budget_for(model),
        "Read notes.txt and tell me its first line.",
        [ToolSpec(TOOL_NAME, "Read a file.", SCHEMA)],
    )
    call = {"type": "tool_use", "id": CALL_ID, "name": TOOL_NAME, "input": {"path": "notes.txt"}}
    result = {"type": "tool_result", "tool_use_id": CALL_ID, "content": "EACCES"}
    body["messages"] += [
        {"role": "assistant", "content": [call]},
        {"role": "user", "content": [{**result, "is_error": is_error}]},
    ]
    return body


@pytest.mark.case_id("anthropic_messages.tool_result_error_rendered")
def test_tool_result_error_flag_reaches_the_model(client, surface, model, ingress):
    sizes = []
    for is_error in (False, True):
        exchange = client.post(surface.path, json_body=_body(surface, model, is_error))
        completion = surface.parse_completion(check_json_response(exchange, ingress))
        check_identity(completion, surface, model)
        check_call_markup(completion, surface, model)
        sizes.append(prompt_size(surface, completion))
    assert_prompt_grows("tool_result_error_rendered", sizes[0], sizes[1], 1)
