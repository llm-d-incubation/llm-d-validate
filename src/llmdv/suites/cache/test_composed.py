"""A coding-agent session on Messages that sends every part a real agent sends, together.

Each part has its own case and passes there alone. An agent sends them in one request: a query
string on the path, a beta header, a block-form system prompt with a cache marker and an
attribution line, tools and a tool exchange in the history, thinking with an effort, metadata,
a large output budget and a system entry inside the history, all streamed. A stack can serve
each part alone and still refuse them together, or serve them and lose the session's reuse.
Only the parts the profile declares are sent, so the case composes what each ingress accepts.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from ...evidence import journal
from ...http.client import RecordingClient
from ...profile.schema import Ingress, Model
from ...surfaces.base import Completion, Surface
from ...surfaces.conversation import Conversation
from ..conftest import check_completion, check_sse_response
from .helpers import Observation, cache_config, check_cold, check_replicas, observation
from .sessions import agent_budget, check_turn, opening, question

# The query string an agent adds to every Messages request.
AGENT_QUERY = "beta=true"
REMINDER = "Reminder: answer from the reference only."


def composed_fields(
    ingress: Ingress, model: Model, probe: str, prefix_words: int
) -> dict[str, Any]:
    """The top-level fields an agent adds, each only where the profile declares it."""
    declared = ingress.capabilities
    fields: dict[str, Any] = {
        "max_tokens": agent_budget(model, ingress, prefix_words),
        "metadata": {"user_id": f"user_{probe}"},
    }
    if "adaptive" in declared.messages_thinking:
        fields["thinking"] = {"type": "adaptive"}
    elif "enabled" in declared.messages_thinking:
        fields["thinking"] = {"type": "enabled", "budget_tokens": 1024}
    if "high" in declared.messages_efforts:
        fields["output_config"] = {"effort": "high"}
    return fields


def composed_turn(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    request: tuple[Conversation, dict[str, Any], dict[str, str]],
    label: str,
) -> tuple[Observation, Completion]:
    """Stream one composed request to the agent's path and read its cache counters."""
    conversation, fields, headers = request
    body = surface.conversation_body(model.public_id, conversation, 16, stream=True)
    body.update(fields)
    if "thinking" in body:
        # The native API refuses a caller-selected temperature with thinking on.
        body.pop("temperature", None)
    exchange, transcript = client.stream(
        f"{surface.path}?{AGENT_QUERY}",
        json_body=body,
        headers=headers,
        stop_event=surface.stream_stop,
    )
    check_sse_response(exchange, ingress)
    surface.assert_stream(transcript)
    completion = surface.assemble([surface.parse_chunk(e.json()) for e in transcript.data_events])
    check_completion(completion, surface, model)
    usage = surface.stream_cache_usage(transcript)
    config = ingress.capabilities.cache_reuse
    return observation(label, usage, exchange, config), completion


@pytest.mark.surface("messages")
@pytest.mark.needs("cache_reuse")
@pytest.mark.case_id("cache.composed_session")
def test_composed_session(client, surface, model, ingress):
    """Two turns of a composed agent session: both served, the second reads the first."""
    config = cache_config(ingress, surface)
    probe = uuid.uuid4().hex
    conversation = opening(config, model)
    if config.stripped_system_prefix is not None:
        line = f"{config.stripped_system_prefix} version=1.0.0; entry=cli; value={probe[:5]};"
        conversation = dataclasses.replace(conversation, system=(line, *conversation.system))
    fields = composed_fields(ingress, model, probe, config.prefix_words)
    betas = ingress.capabilities.anthropic_betas
    headers = {"anthropic-beta": ",".join(betas)} if betas else {}
    inline = "inline_system" in ingress.capabilities.scenario_checks
    journal.record(
        "composed_parts",
        {
            "query": AGENT_QUERY,
            "beta_header": bool(betas),
            "attribution_line": config.stripped_system_prefix is not None,
            "tools": len(conversation.tools),
            "thinking": fields.get("thinking", {}).get("type"),
            "effort": fields.get("output_config", {}).get("effort"),
            "max_tokens": fields["max_tokens"],
            "inline_system": inline,
        },
    )
    first, reply = composed_turn(
        client, surface, model, ingress, (conversation, fields, headers), "turn 1"
    )
    check_cold(first, config)
    # The reply goes back with its thinking, as an agent passes it back.
    later = conversation.replied(reply)
    if reply.tool_calls:
        later = later.tool_results(reply.tool_calls, "No such file.")
    if inline:
        later = later.inline_system(REMINDER)
    later = later.user(question(2))
    if config.stripped_system_prefix is not None:
        line = f"{config.stripped_system_prefix} version=1.0.0; entry=cli; value={probe[5:10]};"
        later = dataclasses.replace(later, system=(line, *later.system[1:]))
    second, _ = composed_turn(client, surface, model, ingress, (later, fields, headers), "turn 2")
    check_replicas([first, second], config)
    check_turn(first, second, config)
