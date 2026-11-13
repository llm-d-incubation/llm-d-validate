"""Reuse when a client changes its tool choice and keeps its tools.

Agents send their tools on every request and change only the choice, such as `none` on a turn
that must answer in text. A template renders the tools whatever the choice, so the prompt is
unchanged. A layer that renders the prompt itself and leaves the tools out when the choice is
`none` places that request without the prefix the earlier one computed.
"""

from __future__ import annotations

import pytest

from ...evidence import journal
from .helpers import cache_config, check_cold, check_replicas, check_warm
from .sessions import observe_on, offers_tools, opening

pytestmark = [pytest.mark.surface_generic("cache_usage"), pytest.mark.needs("cache_reuse")]

# The `none` choice in each surface's form; Chat and Responses take the bare string.
NONE_CHOICE = {"messages": {"type": "none"}}


@pytest.mark.case_id("cache.tool_choice_reuse")
def test_tool_choice_reuse(client, surface, model, ingress):
    """Tools and history with the choice set to none read what automatic choice computed."""
    if not offers_tools(model):
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no tool parser with automatic choice"
        )
    config = cache_config(ingress, surface)
    conversation = opening(config, model)
    choice = {"tool_choice": NONE_CHOICE.get(surface.name, "none")}
    journal.record("tool_choice_change", {"first": "automatic", "repeat": choice["tool_choice"]})
    first = observe_on(client, surface, model, ingress, conversation, "automatic choice")
    repeat = observe_on(client, surface, model, ingress, conversation, "choice none", choice)
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)
