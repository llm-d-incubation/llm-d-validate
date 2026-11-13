"""A salt must isolate an otherwise identical prefix, which stays warm for its own salt."""

import uuid

import pytest

from ...assertions.cache import assert_cold
from ...evidence import journal
from ...profile.schema import CacheReuse
from .helpers import (
    Observation,
    cache_config,
    check_cold,
    check_replicas,
    check_warm,
    observe,
    probe_prompt,
)
from .sessions import check_turn, opening, question, session_turn


@pytest.mark.surface_generic("cache_usage")
@pytest.mark.needs("cache_reuse")
@pytest.mark.case_id("cache.salt_isolation")
def test_cache_salt_isolation(client, surface, model, ingress):
    config = cache_config(ingress, surface)
    if not ingress.capabilities.supports_option("cache_salt"):
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no cache_salt probe on {surface.name}"
        )
    if config.block_size is None and config.cold_max_ratio is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares neither block_size nor cold_max_ratio"
        )
    unsalted = surface.minimal_body(model.public_id, max_tokens=16, prompt=probe_prompt(config))
    salted_a = {**unsalted, "cache_salt": uuid.uuid4().hex}
    sent = [
        (salted_a, "salt A"),
        ({**unsalted, "cache_salt": uuid.uuid4().hex}, "salt B"),
        (unsalted, "no salt"),
        (salted_a, "salt A again"),
    ]
    first, other, bare, again = [
        observe(client, surface, model, ingress, body, label) for body, label in sent
    ]
    check_replicas([first, other, bare, again], config)
    check_cold(first, config)
    # The warm control: without it, a fleet that caches nothing passes every isolation check.
    check_warm(first, again, config)
    for item in (other, bare):
        check_isolated(item, config)


def check_isolated(observation: Observation, config: CacheReuse) -> None:
    """Exactly zero with a declared block size; otherwise at most the declared cold ratio."""
    if config.block_size is not None:
        assert_cold(observation.label, observation.read)
        return
    ok = observation.ratio <= config.cold_max_ratio
    journal.record(
        "cache_salt_isolation",
        {"request": observation.label, "ratio": observation.ratio},
        f"at most {config.cold_max_ratio}",
        ok=ok,
    )
    assert ok, f"{observation.label} reused the salted prefix above the declared cold threshold"


@pytest.mark.surface_generic("cache_usage")
@pytest.mark.needs("cache_reuse")
@pytest.mark.case_id("cache.salted_session")
def test_salted_session(client, surface, model, ingress):
    """A salted session reads its own earlier turn on every turn, and nothing under another salt.

    A layer that forwards the salt on a first request but loses it once the request carries
    history breaks reuse mid-session, or worse, isolation.
    """
    config = cache_config(ingress, surface)
    if not ingress.capabilities.supports_option("cache_salt"):
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no cache_salt probe on {surface.name}"
        )
    if config.block_size is None and config.cold_max_ratio is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares neither block_size nor cold_max_ratio"
        )
    salt, other = uuid.uuid4().hex, uuid.uuid4().hex
    session = opening(config, model, tools=False)
    first, reply = session_turn(client, surface, model, ingress, session, "turn 1", cache_salt=salt)
    session = session.replied(reply).user(question(2))
    second, _ = session_turn(client, surface, model, ingress, session, "turn 2", cache_salt=salt)
    foreign, _ = session_turn(
        client, surface, model, ingress, session, "turn 2 under another salt", cache_salt=other
    )
    check_replicas([first, second, foreign], config)
    check_cold(first, config)
    check_turn(first, second, config)
    check_isolated(foreign, config)
