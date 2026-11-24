"""The same conversation must reach the model the same size through every layer."""

import pytest

from ...assertions.context import assert_same_prompt_size, prompt_size
from ...plugin import register_client
from ...profile.schema import IngressKind
from ..conftest import budget_for, check_basic_completion
from .helpers import mid_system_request

FILLER_WORDS = 300


def _body(surface, model):
    body = mid_system_request(
        surface, model, "Reference note. " + "item " * FILLER_WORDS, budget_for(model)
    )
    body["system"] = "You are a helpful assistant. " + "rule " * FILLER_WORDS
    return body


@pytest.mark.surface("messages")
@pytest.mark.needs("messages_checks:prompt_accounting")
@pytest.mark.case_id("anthropic_messages.prompt_size_matches_engine")
def test_prompt_size_matches_the_engine(
    request, client, client_factory, profile, surface, model, ingress
):
    behind = profile.topology().behind(ingress.id)
    engines = [
        candidate
        for candidate in (profile.ingress(name) for name in behind)
        if candidate.kind is IngressKind.ENGINE
        and surface.name in candidate.surfaces
        and "prompt_accounting" in candidate.capabilities.messages_checks
    ]
    # Only an engine behind this layer can be the one it forwards to. Two engines behind it may
    # serve different models, or different phases of one request, so the comparison is only
    # made when there is exactly one.
    if len(engines) != 1:
        found = ", ".join(repr(item.id) for item in engines) or "none"
        pytest.skip(
            f"[undeclared] comparing {ingress.id!r} needs exactly one engine ingress behind it "
            f"declaring prompt_accounting on Messages; found {found}"
        )
    (engine,) = engines
    reference = prompt_size(
        surface,
        check_basic_completion(
            client_factory(engine.id).post(
                surface.path,
                json_body=_body(surface, engine.model),
                headers=surface.extra_headers(),
            ),
            surface,
            engine.model,
            engine,
        ),
    )
    # The factory attached the engine's client to this case. The verdict is about this ingress,
    # so hand the case back to its own client; the engine's size stays in the recorded check.
    register_client(request.node, ingress, client)
    own = prompt_size(
        surface,
        check_basic_completion(
            client.post(surface.path, json_body=_body(surface, model)), surface, model, ingress
        ),
    )
    assert_same_prompt_size(own, reference, engine.id)
