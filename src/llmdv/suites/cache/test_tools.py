"""Repeats that offer tools: their definitions are part of the prefix a repeat must reuse.

Each request opens with a system prompt led by the case's probe, and every tool description
carries the probe too. A template may render a fixed tools section (its instructions for calling
tools) between the system prompt and the tool schemas, or open with a fixed line; without a
leading system prompt those fixed tokens are the prompt's first blocks, shared by every tools
request, and a first request reads them cached however fresh its own text is. The probe at the
first token keeps the first block unique, so a first request that reads anything read a prefix
an earlier request computed.
"""

from __future__ import annotations

import uuid

import pytest

from ...evidence import journal
from ...profile.schema import CacheReuse, Ingress, Model
from ...surfaces.base import Surface, ToolSpec
from ...surfaces.conversation import Conversation
from ...surfaces.tool_wire import tool_wire
from ..conftest import budget_for, nonce_document
from .helpers import cache_config, check_cold, check_replicas, check_warm, observe, probe_prompt
from .sessions import check_turn

pytestmark = [
    pytest.mark.surface_generic("cache_usage", "tools"),
    pytest.mark.needs("cache_reuse", "tools"),
]


def tool_config(ingress: Ingress, surface: Surface, model: Model) -> CacheReuse:
    config = cache_config(ingress, surface)
    # No tool choice is sent, so the engine must accept automatic choice.
    if not model.tools.auto_choice:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no automatic tool choice")
    return config


def offering(
    surface: Surface, model: Model, probe: str, prompt: str, tools: list[ToolSpec]
) -> dict:
    """A request offering `tools`, its system prompt led by `probe`, asking `prompt`."""
    system = f"Session {probe}. You are an assistant that may call the tools offered."
    conversation = Conversation(system=(system,), tools=tuple(tools)).user(prompt)
    return surface.conversation_body(model.public_id, conversation, budget_for(model))


def city_schema(*, keys_reversed: bool = False) -> dict:
    """One schema, its keys at every level in declaration order or reversed."""
    properties = {
        "city": {"type": "string", "description": "City name."},
        "unit": {"type": "string", "enum": ["c", "f"]},
    }
    schema = {"type": "object", "properties": properties, "required": ["city"]}
    if keys_reversed:
        schema = dict(reversed(schema.items()))
        schema["properties"] = dict(reversed(properties.items()))
    return schema


@pytest.mark.case_id("cache.tools_reuse")
def test_tools_cache_reuse(client, surface, model, ingress):
    """A repeat offering the same two tools reads its whole prefix, tools included."""
    config = tool_config(ingress, surface, model)
    # The probe leads the system prompt and each description, so whether a template renders
    # the system prompt or the tools first, the first block is new, even with a small block size.
    probe = uuid.uuid4().hex
    tools = [
        ToolSpec("get_weather", f"Probe {probe}. Current weather for a city.", city_schema()),
        ToolSpec("get_time", f"Probe {probe}. Current local time for a city.", city_schema()),
    ]
    body = offering(surface, model, probe, probe_prompt(config), tools)
    first, repeat = [
        observe(client, surface, model, ingress, body, label) for label in ("first", "repeat")
    ]
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)


@pytest.mark.case_id("cache.tool_key_order")
def test_tool_key_order_cache_reuse(client, surface, model, ingress):
    """The same tool with its schema keys reordered renders the same prompt and reuses it all."""
    config = tool_config(ingress, surface, model)
    if not config.canonical_tool_keys:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no canonical_tool_keys")
    probe = uuid.uuid4().hex
    description = f"Probe {probe}. Current weather for a city."
    prompt = probe_prompt(config)

    def offer(schema: dict) -> dict:
        # One probe and prompt for both, so the repeat differs in key order alone.
        return offering(
            surface, model, probe, prompt, [ToolSpec("get_weather", description, schema)]
        )

    ordered, reordered = city_schema(), city_schema(keys_reversed=True)
    journal.record("tool_schema_keys", [list(ordered), list(reordered)])
    first = observe(client, surface, model, ingress, offer(ordered), "first")
    repeat = observe(client, surface, model, ingress, offer(reordered), "reordered keys")
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)


MANY_TOOLS = 64


@pytest.mark.case_id("cache.many_tools_reuse")
def test_large_toolset_cache_reuse(client, surface, model, ingress):
    """A repeat offering 64 tools, as an IDE agent with many integrations does, reads it all.

    A large tool block is where a render that orders tools by a hash, or a router that gives up
    tokenizing, shows first; two tools are too few to see either.
    """
    config = tool_config(ingress, surface, model)
    probe = uuid.uuid4().hex
    tools = [
        ToolSpec(f"tool_{index:02d}", f"Probe {probe}. Workspace action {index}.", city_schema())
        for index in range(MANY_TOOLS)
    ]
    journal.record("offered_tools", len(tools))
    body = offering(surface, model, probe, probe_prompt(config), tools)
    first, repeat = [
        observe(client, surface, model, ingress, body, label) for label in ("first", "repeat")
    ]
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)


SEARCH_PROMPT = (
    "Find a tool that reports the weather with tool_search, then use the tool it found to get "
    "the weather in Paris."
)


@pytest.mark.surface("messages")
@pytest.mark.case_id("cache.tool_search_reuse")
def test_tool_search_cache_reuse(client, surface, model, ingress):
    """A tool search that finds a tool keeps the session's prefix: each turn reads the last.

    Claude Code offers its search tool and defers the rest (`defer_loading`). The search
    answers with a `tool_reference`, rendered where the result sits in the history, so the
    head of the prompt never changes. A render that lists a found tool with the offered tools
    at the head instead changes the head the turn a search finds one, and the session
    recomputes everything from there; one that renders a reference differently once it is no
    longer the last turn loses the turn after.
    """
    config = tool_config(ingress, surface, model)
    if ingress.capabilities.messages_features.get("tool_reference") != "accepted":
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} does not declare Messages feature "
            "'tool_reference' accepted"
        )
    probe = uuid.uuid4().hex
    search = ToolSpec(
        "tool_search",
        f"Probe {probe}. Search the available tools by keyword. Returns a reference to each "
        "matching tool.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    weather = ToolSpec("get_weather", f"Probe {probe}. Current weather for a city.", city_schema())
    prompt = f"{nonce_document(config.prefix_words)}\n\n{SEARCH_PROMPT}"
    body = offering(surface, model, probe, prompt, [search, weather])
    for tool in body["tools"]:
        if tool["name"] == weather.name:
            tool["defer_loading"] = True
    search_id, weather_id = f"toolu_{probe[:12]}", f"toolu_{probe[12:24]}"
    reference = tool_wire(surface).tool_reference(weather.name)
    journal.record("tool_search_session", {"deferred": [weather.name], "referenced": weather.name})

    def exchange(call_id: str, name: str, arguments: dict, result: object) -> list[dict]:
        return [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": call_id, "content": result}],
            },
        ]

    found = {
        **body,
        "messages": body["messages"]
        + exchange(search_id, search.name, {"query": "weather"}, [reference]),
    }
    called = {
        **found,
        "messages": found["messages"]
        + exchange(weather_id, weather.name, {"city": "Paris"}, "Paris: sunny, 21 C."),
    }
    observations = [
        observe(client, surface, model, ingress, request, label)
        for request, label in (
            (body, "before the search"),
            (found, "search found a tool"),
            (called, "found tool called"),
        )
    ]
    check_replicas(observations, config)
    check_cold(observations[0], config)
    for previous, current in zip(observations, observations[1:], strict=False):
        check_turn(previous, current, config)
