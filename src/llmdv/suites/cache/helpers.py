"""Shared cache observations, replica control and the declared warm and cold rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from ...assertions import assert_healthy_finish, assert_usage_present_and_consistent
from ...assertions.cache import assert_block_floor, assert_cold, cache_counts
from ...evidence import journal
from ...http.client import Exchange, RecordingClient
from ...http.redact import SENTINEL
from ...profile.schema import CacheReuse, Ingress, Model
from ...surfaces.base import Completion, Surface
from ...surfaces.cache import CacheUsage
from ..conftest import (
    check_basic_completion,
    check_call_markup,
    check_identity,
    check_json_response,
    nonce_document,
)
from ..streaming.helpers import stream_generation


@dataclass(frozen=True)
class Observation:
    """One request's native cache counts and, when declared, the replica that served it."""

    label: str
    read: int
    total: int
    replica: str | None = None

    @property
    def ratio(self) -> float:
        return self.read / self.total


def cache_config(ingress: Ingress, surface: Surface) -> CacheReuse:
    """The ingress's cache declaration for this surface, recorded, or an undeclared skip."""
    config = ingress.capabilities.cache_reuse
    if surface.name not in config.surfaces:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no cache reuse on {surface.name}"
        )
    placement = config.affinity or ("replica_header" if config.replica_header else "single_replica")
    journal.record("cache_contract", {"placement": placement, "block_size": config.block_size})
    return config


def probe_prompt(config: CacheReuse) -> str:
    return f"{nonce_document(config.prefix_words)}\n\nReply with the word ok."


def observation(
    label: str, usage: CacheUsage, exchange: Exchange, config: CacheReuse
) -> Observation:
    read, total = cache_counts(usage)
    headers = {name.lower(): value for name, value in exchange.response_headers.items()}
    return Observation(label, read, total, headers.get(config.replica_header))


def observe(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    body: dict[str, Any],
    label: str,
    *,
    allow_empty: bool = False,
) -> Observation:
    exchange = client.post(surface.path, json_body=body)
    check = check_unanswered_completion if allow_empty else check_basic_completion
    completion = check(exchange, surface, model, ingress)
    usage = surface.parse_cache_usage(completion.raw)
    return observation(label, usage, exchange, ingress.capabilities.cache_reuse)


def observe_stream(
    client: RecordingClient,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    body: dict[str, Any],
    label: str,
    *,
    idle_timeout: float | None = None,
) -> Observation:
    exchange, transcript, _ = stream_generation(
        client, surface, model, ingress, body, idle_timeout=idle_timeout
    )
    usage = surface.stream_cache_usage(transcript)
    return observation(label, usage, exchange, ingress.capabilities.cache_reuse)


def check_unanswered_completion(
    exchange: Exchange, surface: Surface, model: Model, ingress: Ingress
) -> Completion:
    """The shared generation contract without the output check, for a one-token budget.

    One token may open a reasoning block or be whitespace, so an empty answer is no defect.
    """
    completion = surface.parse_completion(check_json_response(exchange, ingress))
    surface.native_contract(completion.raw)
    check_identity(completion, surface, model)
    assert_healthy_finish(completion)
    check_call_markup(completion, surface, model)
    assert_usage_present_and_consistent(completion)
    content, reasoning = len(completion.text.strip()), len(completion.reasoning_text.strip())
    journal.record(
        "output_chars",
        f"content={content} reasoning={reasoning}",
        "any; one token may carry no answer",
    )
    return completion


def check_replicas(observations: list[Observation], config: CacheReuse) -> None:
    if not config.replica_header:
        return
    identities = [item.replica for item in observations]
    valid = all(
        isinstance(value, str) and value.strip() and SENTINEL not in value for value in identities
    )
    journal.record("cache_replica_identity", identities, "visible replica identities", ok=valid)
    assert valid, "declared replica identity header is missing or redacted"
    if len(set(identities)) != 1:
        pytest.skip(
            "[requires_instrumentation] requests reached different replicas; reuse is inconclusive"
        )


def check_cold(observation: Observation, config: CacheReuse) -> None:
    """With a declared block size, a fresh prefix reads nothing; otherwise it is not judged."""
    if config.block_size is not None:
        assert_cold(observation.label, observation.read)


def check_warm(first: Observation, repeat: Observation, config: CacheReuse) -> None:
    """A repeat reads the whole-block floor of its first request, or the declared ratio."""
    if config.block_size is not None:
        assert_block_floor(
            repeat.label, repeat.read, first.total, config.block_size, config.withheld_blocks
        )
        return
    journal.record(
        "cache_reuse_ratio",
        {"warm": first.ratio, "measured": repeat.ratio},
        f"measured >= {config.min_ratio}",
        ok=repeat.ratio >= config.min_ratio,
    )
    assert repeat.ratio >= config.min_ratio, (
        f"cache reuse {repeat.ratio:.3f} is below declared {config.min_ratio:.3f}"
    )
