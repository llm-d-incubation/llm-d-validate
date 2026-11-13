"""A reasoning setting on one API that renders like a setting on another.

Some templates render a Messages thinking mode and a Chat effort as the same prompt tokens, so a
client can move a session between the two APIs warm. Where the profile declares such a pair, a
request with one setting must read the prefix a request with the other computed. A render that
writes each API's setting in its own words ahead of the conversation reads nothing.

A Chat effort also reaches the template as a template kwarg, the form some harnesses send. The
engine renders both forms alike, so a router that renders the prompt itself must too.
"""

from __future__ import annotations

from typing import Any

import pytest

from ... import surfaces
from ...evidence import journal
from ...profile.schema import UNSET_EFFORT, EffortRendering, parse_setting
from .helpers import cache_config, check_cold, check_replicas, check_warm
from .sessions import check_turn, observe_on
from .test_render import single_block

pytestmark = [pytest.mark.surface_generic("cache_usage"), pytest.mark.needs("cache_reuse")]

# Room for the answer beyond a Messages `enabled` thinking budget.
ENABLED_ANSWER_TOKENS = 1024


def setting_fields(entry: str) -> tuple[str, dict[str, Any]]:
    """The surface a `surface:setting` entry names and the request fields that carry it.

    Messages `enabled` sends the entry's thinking budget, which a gateway can map onto an
    effort, with `max_tokens` above it as the API requires.
    """
    parsed = parse_setting(entry)
    if parsed is None:
        raise ValueError(f"malformed equivalent_settings entry {entry!r}")
    name, value, budget = parsed
    if name == "chat_completions":
        return name, {"reasoning_effort": value}
    if name == "responses":
        return name, {"reasoning": {"effort": value}}
    if budget is not None:
        thinking = {"type": value, "budget_tokens": budget}
        return name, {"thinking": thinking, "max_tokens": budget + ENABLED_ANSWER_TOKENS}
    return name, {"thinking": {"type": value}}


@pytest.mark.case_id("cache.setting_equivalence")
def test_setting_equivalence(client, surface, model, ingress):
    """A session started with an equivalent setting on another API continues here warm."""
    config = cache_config(ingress, surface)
    here = f"{surface.name}:"
    orders = [
        (first, second) if second.startswith(here) else (second, first)
        for first, second in config.equivalent_settings
        if here in (first[: len(here)], second[: len(here)])
    ]
    if not orders:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no equivalent setting for {surface.name}"
        )
    journal.record("equivalent_settings", orders)
    for first, _ in orders:
        name = first.partition(":")[0]
        if not ingress.exposes(name):
            pytest.skip(f"[undeclared] ingress {ingress.id!r} does not expose {name}")
    for first, second in orders:
        conversation = single_block(config.prefix_words)
        name, fields = setting_fields(first)
        before = observe_on(client, surfaces.get(name), model, ingress, conversation, first, fields)
        _, fields = setting_fields(second)
        after = observe_on(client, surface, model, ingress, conversation, second, fields)
        check_replicas([before, after], config)
        check_cold(before, config)
        # The same tokens up to the assistant opening, which an API may render its own way.
        check_turn(before, after, config)


def template_effort(efforts: tuple[str, ...], rendering: EffortRendering | None) -> str | None:
    """The first effort, quiet ones first, that the template takes as itself.

    A template kwarg reaches the template as sent, while a deployment's effort map (vLLM's
    `--reasoning-effort-map`) rewrites only the top-level field: an effort the map folds onto
    another is then refused by the template as a kwarg, by design. `effort_rendering` shows the
    fold: an effort whose marker another requested effort also renders may be a map source,
    unless the marker spells the effort itself. A marker that is unique, absent or none gives
    no sign of a fold. Without Chat `effort_rendering`, the first effort is taken.
    """
    if rendering is None or "chat_completions" not in rendering.surfaces:
        return efforts[0]
    markers = {key: value for key, value in rendering.efforts.items() if key != UNSET_EFFORT}
    for effort in efforts:
        marker = markers.get(effort)
        if marker is None or marker.lower() == effort.lower():
            return effort
        if [key for key, value in markers.items() if value == marker] == [effort]:
            return effort
    return None


@pytest.mark.surface("chat_completions")
@pytest.mark.case_id("cache.kwargs_effort_reuse")
def test_kwargs_effort_reuse(client, surface, model, ingress):
    """An effort sent as a template kwarg reads what the same top-level effort computed."""
    config = cache_config(ingress, surface)
    capabilities = ingress.capabilities
    if not capabilities.supports_option("chat_template_kwargs"):
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare chat_template_kwargs")
    efforts = (*capabilities.chat_quiet_efforts, *capabilities.chat_reasoning_efforts)
    if not efforts:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no Chat effort")
    effort = template_effort(efforts, capabilities.effort_rendering)
    if effort is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no Chat effort its effort_rendering "
            "shows the template takes as itself"
        )
    forms = [
        ("top-level effort", {"reasoning_effort": effort}),
        ("template kwarg effort", {"chat_template_kwargs": {"reasoning_effort": effort}}),
    ]
    journal.record("kwargs_effort", effort, "an effort the template takes as itself")
    journal.record("effort_forms", [fields for _, fields in forms])
    conversation = single_block(config.prefix_words)
    first, repeat = [
        observe_on(client, surface, model, ingress, conversation, label, fields)
        for label, fields in forms
    ]
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)
