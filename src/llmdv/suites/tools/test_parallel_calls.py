"""Several calls in one turn, the switch that disables them, and the same calls streamed."""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...evidence import journal
from ...surfaces.tool_wire import ToolChoice
from .common import (
    ASK_BOTH,
    ASK_THREE,
    NEWS,
    TIME,
    WEATHER,
    check_call_turn,
    post_tools,
    require_auto,
    stream_tools,
    tool_budget,
    wire,
)

pytestmark = [pytest.mark.surface_generic("tools"), pytest.mark.needs("tools")]

# Independent samples with no retry: a merged-call defect shows in some streams only.
PARALLEL_SAMPLES = 3


# A forced choice decodes every call through one constrained grammar, and auto parses the
# model's own markup, so several calls take a different path under each.
@pytest.mark.parametrize("mode", ["auto", "required"])
@pytest.mark.case_id("tools.parallel")
def test_parallel_calls(client, surface, model, ingress, mode):
    """With parallel calls allowed, two needed tools come back as two separate calls."""
    if mode == "auto":
        require_auto(model, ingress)
    tools, choice = [WEATHER, TIME], ToolChoice(mode)
    body = wire(surface).request(
        model.public_id, ASK_BOTH, tools, choice, tool_budget(model), parallel=True
    )
    completion = post_tools(client, surface, ingress, body)
    check_call_turn(completion, surface, model, tools, choice, names=["get_time", "get_weather"])


@pytest.mark.case_id("tools.parallel_three")
def test_three_parallel_calls(client, surface, model, ingress):
    """Three needed tools come back as three calls, each with its own id and arguments."""
    require_auto(model, ingress)
    tools, choice = [WEATHER, TIME, NEWS], ToolChoice("auto")
    body = wire(surface).request(
        model.public_id, ASK_THREE, tools, choice, tool_budget(model), parallel=True
    )
    completion = post_tools(client, surface, ingress, body)
    check_call_turn(
        completion, surface, model, tools, choice, names=["get_news", "get_time", "get_weather"]
    )


@pytest.mark.parametrize("mode", ["auto", "required"])
@pytest.mark.case_id("tools.parallel_disabled")
def test_parallel_disabled(client, surface, model, ingress, mode):
    """With parallel calls disabled, a prompt asking for two tools gets exactly one call,
    whether the choice is left to the model or forced."""
    if mode == "auto":
        require_auto(model, ingress)
    tools, choice = [WEATHER, TIME], ToolChoice(mode)
    body = wire(surface).request(
        model.public_id, ASK_BOTH, tools, choice, tool_budget(model), parallel=False
    )
    completion = post_tools(client, surface, ingress, body)
    check_call_turn(completion, surface, model, tools, choice, maximum=1)


@pytest.mark.parametrize("mode", ["auto", "required"])
@pytest.mark.case_id("tools.parallel_stream")
def test_parallel_stream(client, surface, model, ingress, mode):
    """Two streamed calls stay two calls in every sample, each with valid arguments and an id
    no other response reused."""
    if mode == "auto":
        require_auto(model, ingress)
    tools, choice = [WEATHER, TIME], ToolChoice(mode)
    seen_ids: list[str] = []
    for sample in range(PARALLEL_SAMPLES):
        body = wire(surface).request(
            model.public_id,
            ASK_BOTH,
            tools,
            choice,
            tool_budget(model),
            parallel=True,
            stream=True,
            sampled=True,
        )
        journal.record("sample", sample)
        completion = stream_tools(client, surface, ingress, body)
        calls = check_call_turn(
            completion, surface, model, tools, choice, names=["get_time", "get_weather"]
        )
        seen_ids += [call.id for call, _ in calls]
    if len(set(seen_ids)) != len(seen_ids):
        fail("tool_call_ids_across_responses", seen_ids, "no id repeated across responses")
    journal.record("ids_across_responses", len(seen_ids), "all distinct", ok=True)
