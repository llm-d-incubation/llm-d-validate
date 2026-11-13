"""A fresh prefix sent twice, in each shape a client sends it; no retries or metric inference."""

import pytest

from .helpers import (
    cache_config,
    check_cold,
    check_replicas,
    check_warm,
    observe,
    observe_stream,
    probe_prompt,
)


@pytest.mark.surface_generic("cache_usage")
@pytest.mark.needs("cache_reuse")
@pytest.mark.case_id("cache.native_reuse")
def test_native_cache_reuse(client, surface, model, ingress):
    config = cache_config(ingress, surface)
    body = surface.minimal_body(model.public_id, max_tokens=16, prompt=probe_prompt(config))
    first, repeat = [
        observe(client, surface, model, ingress, body, label) for label in ("first", "repeat")
    ]
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)


@pytest.mark.surface_generic("cache_usage")
@pytest.mark.needs("cache_reuse")
@pytest.mark.case_id("cache.stream_reuse")
def test_streamed_cache_reuse(client, surface, model, ingress):
    """A streamed repeat reports its reuse where a streaming client reads the counters."""
    config = cache_config(ingress, surface)
    body = surface.stream_body(
        model.public_id, max_tokens=16, include_usage=True, prompt=probe_prompt(config)
    )
    first, repeat = [
        observe_stream(client, surface, model, ingress, body, label)
        for label in ("first", "repeat")
    ]
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)


@pytest.mark.surface_generic("cache_usage")
@pytest.mark.needs("cache_reuse")
@pytest.mark.case_id("cache.single_token_reuse")
def test_single_token_cache_reuse(client, surface, model, ingress):
    """A one-token generation, which agents send to warm a cache, still writes and reads it."""
    config = cache_config(ingress, surface)
    body = surface.minimal_body(model.public_id, max_tokens=1, prompt=probe_prompt(config))
    first, repeat = [
        observe(client, surface, model, ingress, body, label, allow_empty=True)
        for label in ("first", "repeat")
    ]
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)
