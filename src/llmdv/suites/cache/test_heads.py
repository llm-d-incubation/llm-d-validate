"""A change at one end of a prompt: at the head it costs everything, at the tail only the tail.

Prefix caches chain each block to the blocks before it. A layer that matches blocks by their
own content serves one conversation's KV to another whose text differs only at the start. A
layer that places a request by its whole system prompt moves a client whose system prompt ends
with a clock line, and that client recomputes everything on every request.
"""

import uuid

import pytest

from ...assertions.cache import assert_cold
from ...evidence import journal
from ...surfaces.conversation import Conversation
from ..conftest import nonce_document
from .helpers import cache_config, check_cold, check_replicas, check_warm, observe, probe_prompt
from .sessions import check_turn, observe_on


@pytest.mark.surface_generic("cache_usage")
@pytest.mark.needs("cache_reuse")
@pytest.mark.case_id("cache.changed_head")
def test_changed_head_reads_nothing(client, surface, model, ingress):
    config = cache_config(ingress, surface)
    if config.block_size is None and config.cold_max_ratio is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares neither block_size nor cold_max_ratio"
        )
    document = probe_prompt(config)

    def headed() -> str:
        return f"Revision {uuid.uuid4().hex}.\n\n{document}"

    original = headed()
    sent = [(original, "first"), (headed(), "changed head"), (original, "first again")]
    first, changed, again = [
        observe(
            client,
            surface,
            model,
            ingress,
            surface.minimal_body(model.public_id, max_tokens=16, prompt=prompt),
            label,
        )
        for prompt, label in sent
    ]
    check_replicas([first, changed, again], config)
    check_cold(first, config)
    # The warm control: without it, a fleet that caches nothing passes the changed-head check.
    check_warm(first, again, config)
    if config.block_size is not None:
        assert_cold(changed.label, changed.read)
        return
    ok = changed.ratio <= config.cold_max_ratio
    journal.record(
        "cache_changed_head",
        {"request": changed.label, "ratio": changed.ratio},
        f"at most {config.cold_max_ratio}",
        ok=ok,
    )
    assert ok, "a prompt with a changed head reused blocks of another prompt"


@pytest.mark.surface_generic("cache_usage")
@pytest.mark.needs("cache_reuse")
@pytest.mark.case_id("cache.changed_tail")
def test_changed_tail_costs_only_the_tail(client, surface, model, ingress):
    """A system prompt whose last line changes per request, as a clock line does, keeps the rest.

    The changed line, the short question after it and the assistant opening fit in one block,
    so the second request must read the first's prompt less two blocks.
    """
    config = cache_config(ingress, surface)
    head = (
        f"Session {uuid.uuid4().hex}. You are a coding agent.",
        nonce_document(config.prefix_words),
    )

    def stamped(minute: int) -> Conversation:
        clock = f"Current time: 2026-01-01T09:{minute:02d}:00Z"
        return Conversation(system=(*head, clock)).user("Reply with the word ok.")

    first, later = [
        observe_on(client, surface, model, ingress, stamped(minute), label)
        for minute, label in ((0, "first"), (1, "new clock line"))
    ]
    check_replicas([first, later], config)
    check_cold(first, config)
    check_turn(first, later, config, slack_blocks=2)
