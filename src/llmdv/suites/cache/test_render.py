"""Reuse that depends on how the fleet renders what a client sends, beyond the conversation.

One session moved between APIs, a per-request attribution line at the head of the system
prompt, the cache hints newer clients add, and fields no template renders, such as sampling and
the end user: each changes the request without changing the prompt a correct render produces,
so each must read what the request before it computed.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from ... import surfaces
from ...evidence import journal
from ...surfaces.conversation import Conversation
from ..conftest import nonce_document
from .helpers import (
    cache_config,
    check_cold,
    check_replicas,
    check_warm,
    observe,
)
from .sessions import check_turn, observe_on, question

pytestmark = [pytest.mark.surface_generic("cache_usage"), pytest.mark.needs("cache_reuse")]

BUDGET = 16


def single_block(words: int) -> Conversation:
    """One system block and one question: the shape every API renders the same way."""
    system = f"Session {uuid.uuid4().hex}. You are a coding agent.\n\n{nonce_document(words)}"
    return Conversation(system=(system,)).user(question(1))


@pytest.mark.case_id("cache.cross_api")
def test_cross_api_reuse(client, surface, model, ingress):
    """A session started on another declared-equivalent API continues here warm."""
    config = cache_config(ingress, surface)
    peers = [
        name
        for name in config.render_equivalent
        if name != surface.name and surface.name in config.render_equivalent
    ]
    if not peers:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no render_equivalent peer "
            f"for {surface.name}"
        )
    journal.record("render_equivalent_peers", peers)
    for name in peers:
        if not ingress.exposes(name):
            pytest.skip(f"[undeclared] ingress {ingress.id!r} does not expose {name}")
    for name in peers:
        conversation = single_block(config.prefix_words)
        first = observe_on(client, surfaces.get(name), model, ingress, conversation, name)
        here = observe_on(client, surface, model, ingress, conversation, surface.name)
        check_replicas([first, here], config)
        check_cold(first, config)
        # The same tokens up to the assistant opening, which an API may render its own way.
        check_turn(first, here, config)


@pytest.mark.case_id("cache.attribution_line")
def test_attribution_line_reuse(client, surface, model, ingress):
    """A per-request attribution line at the head of the system prompt costs nothing.

    Only where the fleet declares it strips that line: anywhere else the value changes the
    first block and the prompt behind it is rightly recomputed.
    """
    config = cache_config(ingress, surface)
    prefix = config.stripped_system_prefix
    if prefix is None:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no stripped_system_prefix")
    document = single_block(config.prefix_words)

    def attributed() -> Conversation:
        line = f"{prefix} version=1.0.0; entry=cli; value={uuid.uuid4().hex[:5]};"
        return dataclasses.replace(document, system=(line, *document.system))

    sent = [attributed(), attributed()]
    journal.record("attribution_lines", [item.system[0] for item in sent])
    first, repeat = [
        observe_on(client, surface, model, ingress, item, label)
        for item, label in zip(sent, ("first", "new attribution value"), strict=True)
    ]
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)


@pytest.mark.needs("cache_reuse", "scenario_checks:cache_hints")
@pytest.mark.case_id("cache.cache_hints")
def test_cache_hints_reuse(client, surface, model, ingress):
    """A repeat that adds the client's cache hints reads what the plain request computed."""
    config = cache_config(ingress, surface)
    plain = single_block(config.prefix_words)
    hinted = dataclasses.replace(plain, cache_key=f"session-{uuid.uuid4().hex}")
    bodies = [
        (surface.conversation_body(model.public_id, item, BUDGET), label)
        for item, label in ((plain, "plain"), (hinted, "with cache hints"))
    ]
    journal.record(
        "cache_hint_fields",
        sorted(set(bodies[1][0]) - set(bodies[0][0])) or "system cache marker ttl",
    )
    first, repeat = [
        observe(client, surface, model, ingress, body, label) for body, label in bodies
    ]
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)


def unrendered_fields(surface: str, kind: str, index: int) -> dict[str, Any]:
    """Sampling, or the end user a client names, in each surface's own field."""
    if kind == "sampling":
        return {"temperature": float(index)}
    user = f"user-{index}-{uuid.uuid4().hex[:8]}"
    return {"metadata": {"user_id": user}} if surface == "messages" else {"user": user}


@pytest.mark.parametrize("kind", ["sampling", "identity"])
@pytest.mark.case_id("cache.unrendered_fields")
def test_unrendered_fields_reuse(client, surface, model, ingress, kind):
    """A repeat that changes only fields no template renders reads what the first computed.

    Many users share one system prompt, and a client changes its temperature between calls. A
    layer that places requests by those fields sends the repeat to a replica without the prefix.
    """
    config = cache_config(ingress, surface)
    conversation = single_block(config.prefix_words)
    sent = [unrendered_fields(surface.name, kind, index) for index in (0, 1)]
    journal.record("unrendered_fields", sent)
    first, repeat = [
        observe_on(client, surface, model, ingress, conversation, label, fields)
        for fields, label in zip(sent, ("first", f"changed {kind}"), strict=True)
    ]
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)
