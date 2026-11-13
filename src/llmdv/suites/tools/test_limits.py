"""Tool turns with a built-in-tool cap or an output limit that cuts a call."""

from __future__ import annotations

import pytest

from ...assertions.tool_calls import assert_calls
from ...evidence import journal
from ...surfaces.tool_wire import ToolChoice, ToolDef
from ..conftest import check_completion
from .common import (
    ASK_BOTH,
    TIME,
    WEATHER,
    check_call_turn,
    enforced_schemas,
    post_tools,
    require_auto,
    served_or_refused,
    tool_budget,
    wire,
)

pytestmark = [pytest.mark.surface_generic("tools"), pytest.mark.needs("tools")]


@pytest.mark.surface("responses")
@pytest.mark.case_id("tools.max_tool_calls")
def test_max_tool_calls(client, surface, model, ingress):
    """Function calls remain valid with max_tool_calls set, or get a clean 4xx refusal."""
    require_auto(model, ingress)
    tools, choice = [WEATHER, TIME], ToolChoice("auto")
    shape = wire(surface)
    body = shape.limit_calls(
        shape.request(model.public_id, ASK_BOTH, tools, choice, tool_budget(model), parallel=True),
        1,
    )
    exchange = client.post(surface.path, json_body=body)
    if served_or_refused(exchange, surface, ingress):
        completion = surface.parse_completion(exchange.response_body)
        # max_tool_calls caps built-in tools, not these client-executed functions.
        check_call_turn(completion, surface, model, tools, choice)


ESSAY = ToolDef(
    "save_essay",
    "Save an essay.",
    {
        "type": "object",
        "properties": {"text": {"type": "string", "minLength": 400}},
        "required": ["text"],
    },
    # Strict, so a finished call with broken arguments is held to the schema (#270).
    strict=True,
)
# Far below what the essay needs, so the output limit cuts the turn.
CUT_BUDGET = 16


@pytest.mark.case_id("tools.truncated_call")
def test_truncated_call(client, surface, model, ingress):
    """A call cut by the output limit ends with the limit's finish reason. A finished call with
    broken arguments fails, since an agent would execute it."""
    choice = ToolChoice("required")
    shape = wire(surface)
    body = shape.request(
        model.public_id,
        "Write a 300-word essay about rivers and pass it to the save_essay tool as text.",
        [ESSAY],
        choice,
        CUT_BUDGET,
    )
    completion = post_tools(client, surface, ingress, body)
    check_completion(completion, surface, model)
    called, cut = shape.call_finish(model, choice), shape.length_finish()
    finish = completion.finish_reason
    journal.record("truncated_finish", finish, f"{cut}, or {called} with a complete call")
    if finish == called:
        assert_calls(
            completion,
            {ESSAY.name: ESSAY.schema},
            enforced=enforced_schemas(model, [ESSAY]),
            id_pattern=shape.id_pattern,
        )
        return
    ok = finish == cut
    journal.record("cut_by_limit", finish, cut, ok=ok)
    assert ok, f"a turn at {CUT_BUDGET} output tokens finished {finish!r}, expected {cut!r}"
