"""The reasoning effort the model is given is the one the profile declares for the request.

A deployment can map a requested effort onto another (vLLM's `--reasoning-effort-map`), and an
effort the template does not know can fall through to its default. Acceptance, output size and
effort order cannot tell which effort the model saw; the rendered prompt can. Each request asks
for one token and `prompt_logprobs: 0`, rebuilds the prompt from it, and reads the effort
marker the profile's `effort_rendering.pattern` names. A request with no effort checks the
template's default the same way.
"""

from __future__ import annotations

from typing import Any

import pytest

from ...assertions.effort_rendering import assert_effort_rendered, rendered_prompt
from ...evidence import journal
from ...http.client import RecordingClient
from ...profile.schema import EFFORT_VALUES, UNSET_EFFORT, EffortRendering, Ingress
from ...surfaces.base import DEFAULT_PROMPT, Surface
from ..conftest import check_json_response

pytestmark = pytest.mark.needs("effort_rendering")


def _config(ingress: Ingress, surface: Surface, effort: str) -> EffortRendering:
    config = ingress.capabilities.effort_rendering
    if surface.name not in config.surfaces:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no effort rendering on {surface.name}"
        )
    if effort not in config.efforts:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no rendered marker for effort {effort!r}"
        )
    journal.record("requested_effort", effort)
    journal.record("effort_pattern", config.pattern)
    return config


def _check(
    client: RecordingClient,
    surface: Surface,
    ingress: Ingress,
    config: EffortRendering,
    effort: str,
    body: dict[str, Any],
) -> None:
    response = check_json_response(client.post(surface.path, json_body=body), ingress)
    assert_effort_rendered(rendered_prompt(response), config, effort)


@pytest.mark.surface("chat_completions")
@pytest.mark.case_id("field_options.chat_effort_rendered")
@pytest.mark.parametrize("effort", (UNSET_EFFORT, *EFFORT_VALUES["chat_completions"]))
def test_chat_effort_rendered(client, surface, model, ingress, effort):
    config = _config(ingress, surface, effort)
    extra = {} if effort == UNSET_EFFORT else {"reasoning_effort": effort}
    body = surface.minimal_body(model.public_id, 1, prompt_logprobs=0, **extra)
    _check(client, surface, ingress, config, effort, body)


@pytest.mark.surface("responses")
@pytest.mark.case_id("field_options.responses_effort_rendered")
@pytest.mark.parametrize("effort", (UNSET_EFFORT, *EFFORT_VALUES["responses"]))
def test_responses_effort_rendered(client, surface, model, ingress, effort):
    config = _config(ingress, surface, effort)
    extra = {} if effort == UNSET_EFFORT else {"reasoning": {"effort": effort}}
    # 16 is the smallest output budget the Responses API documents.
    body = surface.minimal_body(model.public_id, 16, prompt_logprobs=0, **extra)
    _check(client, surface, ingress, config, effort, body)


@pytest.mark.surface("messages")
@pytest.mark.case_id("field_options.messages_effort_rendered")
@pytest.mark.parametrize("effort", (UNSET_EFFORT, *EFFORT_VALUES["messages"]))
def test_messages_effort_rendered(client, surface, model, ingress, effort):
    config = _config(ingress, surface, effort)
    body = surface.thinking_body(
        model.public_id,
        effort=None if effort == UNSET_EFFORT else effort,
        prompt=DEFAULT_PROMPT,
        max_tokens=1,
    )
    body["prompt_logprobs"] = 0
    _check(client, surface, ingress, config, effort, body)
