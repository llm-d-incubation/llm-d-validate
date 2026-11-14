"""Sub-agents fanned out from one session: concurrent requests that share its prefix.

After the shared context is computed once, every sub-agent request must read it, streamed or
not, wherever the ingress places it. A cold fan-out computes the prefix once per request by
design, so the warm-up comes first.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from ...evidence import journal
from ...http.client import Exchange, RecordingClient
from ...http.sse import Transcript
from ...profile.schema import Ingress, Model
from ...surfaces.base import Surface
from ...surfaces.conversation import Conversation
from ..conftest import check_basic_completion, check_completion, check_sse_response
from .helpers import Observation, cache_config, check_cold, check_replicas, observation, observe
from .sessions import check_turn, opening, session_budget

SUBAGENTS = 4

pytestmark = [
    pytest.mark.surface_generic("cache_usage"),
    pytest.mark.needs("cache_reuse", "scenario_checks:parallel_subagents"),
    pytest.mark.disruptive("sends four concurrent generations"),
]


def task(index: int) -> str:
    return f"Sub-agent {index}: name one word from paragraph {index} of the reference only."


def send(
    client: RecordingClient, surface: Surface, body: dict[str, Any]
) -> tuple[Exchange, Transcript | None]:
    """One request, without checks: concurrent workers only send, the case judges after."""
    if body.get("stream"):
        return client.stream(surface.path, json_body=body, stop_event=surface.stream_stop)
    return client.post(surface.path, json_body=body), None


def judged(
    surface: Surface,
    model: Model,
    ingress: Ingress,
    label: str,
    exchange: Exchange,
    transcript: Transcript | None,
) -> Observation:
    config = ingress.capabilities.cache_reuse
    if transcript is None:
        completion = check_basic_completion(exchange, surface, model, ingress)
        return observation(label, surface.parse_cache_usage(completion.raw), exchange, config)
    check_sse_response(exchange, ingress)
    surface.assert_stream(transcript)
    chunks = [surface.parse_chunk(event.json()) for event in transcript.data_events]
    check_completion(surface.assemble(chunks), surface, model)
    return observation(label, surface.stream_cache_usage(transcript), exchange, config)


@pytest.mark.case_id("cache.parallel_subagents")
def test_parallel_subagents_read_the_shared_prefix(client, surface, model, ingress):
    config = cache_config(ingress, surface)
    shared = opening(config, model, tools=False)
    # The session's own question is the parent's turn; each sub-agent replaces it with its task.
    parent = Conversation(system=shared.system)
    budget = session_budget(model)
    warm = observe(
        client,
        surface,
        model,
        ingress,
        surface.conversation_body(model.public_id, parent.user(task(0)), budget),
        "warm-up",
    )
    bodies = [
        surface.conversation_body(
            model.public_id, parent.user(task(index)), budget, stream=index % 2 == 1
        )
        for index in range(1, SUBAGENTS + 1)
    ]
    journal.record(
        "subagent_requests",
        {"count": len(bodies), "streamed": sum(1 for b in bodies if b.get("stream"))},
    )
    with ThreadPoolExecutor(max_workers=SUBAGENTS) as pool:
        sent = list(pool.map(lambda body: send(client, surface, body), bodies))
    subagents = [
        judged(surface, model, ingress, f"sub-agent {index}", *result)
        for index, result in enumerate(sent, start=1)
    ]
    check_replicas([warm, *subagents], config)
    check_cold(warm, config)
    for item in subagents:
        # Each task replaces the warm-up's one-line task, within two blocks of 16 tokens or more.
        check_turn(warm, item, config, slack_blocks=2)
