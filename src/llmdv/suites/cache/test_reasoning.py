"""Each reasoning setting must reuse its own cached prefix.

A setting renders into the prompt, so a layer that renders it differently from the engine, or
cannot see it, routes the repeat without knowing where its prefix is. Two repeats per setting:
a repeat placed at random can still land warm by chance.
"""

from __future__ import annotations

from typing import Any

import pytest

from ...evidence import journal
from ...http.client import RecordingClient
from ...profile.schema import (
    CHAT_REASONING_EFFORTS,
    MESSAGES_EFFORTS,
    MESSAGES_THINKING_MODES,
    RESPONSES_REASONING_EFFORTS,
    Ingress,
    Model,
)
from ...surfaces.base import Surface
from ..conftest import budget_for
from .helpers import cache_config, check_cold, check_replicas, check_warm, observe, probe_prompt

pytestmark = pytest.mark.needs("cache_reuse")


def check_setting_reuse(
    client: RecordingClient, surface: Surface, model: Model, ingress: Ingress, body: dict[str, Any]
) -> None:
    config = ingress.capabilities.cache_reuse
    first, *repeats = [
        observe(client, surface, model, ingress, body, label)
        for label in ("first", "repeat 1", "repeat 2")
    ]
    check_replicas([first, *repeats], config)
    check_cold(first, config)
    for repeat in repeats:
        check_warm(first, repeat, config)


@pytest.mark.surface("chat_completions")
@pytest.mark.case_id("cache.reasoning_effort_reuse")
@pytest.mark.parametrize("effort", CHAT_REASONING_EFFORTS)
def test_reasoning_effort_cache_reuse(client, surface, model, ingress, effort):
    config = cache_config(ingress, surface)
    if effort not in ingress.capabilities.chat_reasoning_efforts:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare Chat effort {effort!r}")
    journal.record("requested_reasoning_effort", effort)
    prompt = probe_prompt(config)
    body = surface.reasoning_body(model.public_id, budget_for(model), effort, prompt=prompt)
    check_setting_reuse(client, surface, model, ingress, body)


@pytest.mark.surface("messages")
@pytest.mark.case_id("cache.thinking_mode_reuse")
@pytest.mark.parametrize("mode", MESSAGES_THINKING_MODES)
def test_thinking_mode_cache_reuse(client, surface, model, ingress, mode):
    config = cache_config(ingress, surface)
    if mode not in ingress.capabilities.messages_thinking:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare thinking mode {mode!r}")
    journal.record("requested_thinking_mode", mode)
    body = surface.thinking_body(model.public_id, mode=mode, prompt=probe_prompt(config))
    check_setting_reuse(client, surface, model, ingress, body)


@pytest.mark.surface("responses")
@pytest.mark.case_id("cache.responses_effort_reuse")
@pytest.mark.parametrize("effort", RESPONSES_REASONING_EFFORTS)
def test_responses_effort_cache_reuse(client, surface, model, ingress, effort):
    config = cache_config(ingress, surface)
    if effort not in ingress.capabilities.responses_reasoning_efforts:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} does not declare Responses effort {effort!r}"
        )
    journal.record("requested_reasoning_effort", effort)
    body = surface.minimal_body(
        model.public_id, budget_for(model), probe_prompt(config), reasoning={"effort": effort}
    )
    check_setting_reuse(client, surface, model, ingress, body)


@pytest.mark.surface("messages")
@pytest.mark.case_id("cache.messages_effort_reuse")
@pytest.mark.parametrize("effort", MESSAGES_EFFORTS)
def test_anthropic_effort_cache_reuse(client, surface, model, ingress, effort):
    config = cache_config(ingress, surface)
    if effort not in ingress.capabilities.messages_efforts:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} does not declare Messages effort {effort!r}"
        )
    journal.record("requested_output_effort", effort)
    body = surface.thinking_body(model.public_id, effort=effort, prompt=probe_prompt(config))
    check_setting_reuse(client, surface, model, ingress, body)
