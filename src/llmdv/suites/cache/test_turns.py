"""Sessions as clients run them: each turn extends the previous prompt and must read it back.

A repeat of one prompt proves the cache works; a session proves the history renders the same
way on every turn and that the next turn is placed where the previous one was computed.
"""

from __future__ import annotations

import uuid

import pytest

from ...evidence import journal
from ...surfaces.conversation import Conversation
from ..conftest import nonce_document
from .helpers import cache_config, check_cold, check_replicas
from .sessions import check_turn, next_turn, opening, question, session_turn

pytestmark = [pytest.mark.surface_generic("cache_usage"), pytest.mark.needs("cache_reuse")]

TURNS = 3


@pytest.mark.case_id("cache.turn_reuse")
@pytest.mark.parametrize("history", ["kept", "stripped"])
def test_turn_reuse(client, surface, model, ingress, history):
    """Three streamed turns, each reply passed back with its reasoning kept or stripped."""
    config = cache_config(ingress, surface)
    journal.record("reasoning_in_history", history)
    conversation = opening(config, model)
    observations = []
    for turn in range(1, TURNS + 1):
        seen, reply = session_turn(client, surface, model, ingress, conversation, f"turn {turn}")
        observations.append(seen)
        conversation = next_turn(conversation, reply, turn + 1, keep_reasoning=history == "kept")
    check_replicas(observations, config)
    check_cold(observations[0], config)
    for previous, current in zip(observations, observations[1:], strict=False):
        check_turn(previous, current, config)


@pytest.mark.needs("cache_reuse", "scenario_checks:inline_system")
@pytest.mark.case_id("cache.inline_system")
def test_inline_system_reuse(client, surface, model, ingress):
    """A system message inside the history is rendered where it was sent.

    Coding agents deliver reminders this way. A render that hoists it to the head of the prompt,
    or merges it into the leading system prompt, moves everything after that point, and every
    turn recomputes the whole conversation. The conversation therefore sits in the first user
    turn behind a one-line system prompt, so either move shifts all of it.
    """
    config = cache_config(ingress, surface)
    probe = uuid.uuid4().hex
    conversation = Conversation(system=(f"Session {probe}. You are a coding agent.",)).user(
        f"{nonce_document(config.prefix_words)}\n\n{question(1)}"
    )
    first, reply = session_turn(client, surface, model, ingress, conversation, "turn 1")
    note = f"Reminder {uuid.uuid4().hex[:8]}: answer in one word."
    journal.record("inline_system_text", note)
    conversation = conversation.replied(reply).inline_system(note).user(question(2))
    second, _ = session_turn(client, surface, model, ingress, conversation, "turn 2")
    check_replicas([first, second], config)
    check_cold(first, config)
    check_turn(first, second, config)


@pytest.mark.case_id("cache.compaction")
def test_compaction_reuse(client, surface, model, ingress):
    """A client that compacts its history keeps its system prompt and rewrites the rest.

    The compacted request must still read the system prefix every turn shared, and the turn
    after it must read the compacted request: compaction costs the history, never the session.
    """
    config = cache_config(ingress, surface)
    session = opening(config, model, tools=False)
    first, reply = session_turn(client, surface, model, ingress, session, "turn 1")
    session = session.replied(reply).user(question(2))
    second, _ = session_turn(client, surface, model, ingress, session, "turn 2")
    summary = (
        f"Summary {uuid.uuid4().hex[:8]} of the conversation so far: the user asked for one "
        f"word from paragraphs 1 and 2 of the reference and got them. {question(3)}"
    )
    compacted = session.compacted(summary)
    journal.record("compacted_turns", {"before": len(session.turns), "after": 1})
    third, reply = session_turn(client, surface, model, ingress, compacted, "compacted")
    compacted = compacted.replied(reply).user(question(4))
    fourth, _ = session_turn(client, surface, model, ingress, compacted, "after compaction")
    check_replicas([first, second, third, fourth], config)
    check_cold(first, config)
    check_turn(first, second, config)
    # The compacted request shares the first turn's prompt up to its one-line question, which
    # with the assistant opening stays within two blocks of 16 tokens or more.
    check_turn(first, third, config, slack_blocks=2)
    check_turn(third, fourth, config)
