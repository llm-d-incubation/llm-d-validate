"""Forward compatibility: what a client release sends before the fleet knows it.

Each case sends one ordinary request carrying one unknown element: a field inside a conversation
entry, a content block or part type, a Responses input item type, a tool type, a request header,
an anthropic-beta value, a new value of the reasoning setting, or a content block on Messages
count_tokens. The ingress declares its outcome under `capabilities.forward_compat`: `accepted`
(a healthy generation), `rejected` (a client error with the surface's envelope) or `either`.
Whatever is declared, a 5xx or a dropped connection fails, because that is how a client release
takes a layer down. Then one plain request on the same surface must succeed, so a probe that
breaks an engine fails here and not in whichever case happens to run next.

Undeclared probes skip as `undeclared` without sending a request. None of these is disruptive:
each is what an ordinary newer client sends.
"""

from __future__ import annotations

import pytest

from ...assertions import assert_not_throttled, fail
from ...assertions.messages import assert_messages_token_count
from ...evidence import journal
from ...profile.evolution import EVERY_SURFACE
from ...surfaces import variants
from ...surfaces.base import DEFAULT_PROMPT
from ..conftest import budget_for, check_basic_completion, check_json_response

pytestmark = pytest.mark.surface_generic


def _declared(ingress, surface, probe: str) -> str:
    per = ingress.capabilities.forward_compat.get(probe, {})
    outcome = per.get(surface.name, per.get(EVERY_SURFACE))
    if outcome is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no forward_compat.{probe} "
            f"for {surface.name}"
        )
    return outcome


def _probe(client, surface, model, ingress, probe: str) -> None:
    outcome = _declared(ingress, surface, probe)
    budget = budget_for(model)
    built = variants.unknown_probe(surface, model.public_id, probe, budget, surface.reply_prompt)
    if built is None:
        pytest.skip(f"[not_implemented] surface {surface.name!r} cannot carry {probe}")
    body, headers = built
    journal.record("forward_compat_probe", probe, f"declared {outcome}")

    exchange = client.post(surface.path, json_body=body, headers=headers)
    _judge(exchange, surface, ingress, probe, outcome)
    if _served(exchange, outcome):
        check_basic_completion(exchange, surface, model, ingress)

    _follow_up(client, surface, model, ingress, budget)


def _served(exchange, outcome: str) -> bool:
    return outcome == "accepted" or (outcome == "either" and 200 <= exchange.status < 300)


def _judge(exchange, surface, ingress, probe: str, outcome: str) -> None:
    """Fail a dropped connection or a 5xx, and check a declared rejection's status.

    The rejection's error envelope is recorded without a verdict. Its shape belongs to the error
    cases and `evolution.invalid_contract`, so a known envelope defect on one surface does not
    hold this case red and hide a later change from a 4xx to a 5xx.
    """
    assert_not_throttled(exchange, ingress)
    if exchange.transport_error is not None:
        fail("probe_transport", exchange.transport_error, "an HTTP response")
    status = exchange.status
    if status >= 500:
        fail("probe_status", status, f"no 5xx for {probe}; declared {outcome}")
    if not _served(exchange, outcome):
        rejected = 400 <= status < 500
        journal.record("probe_status", status, "a 4xx", ok=rejected)
        if not rejected:
            fail("probe_outcome", status, f"a 4xx for {probe}; declared {outcome}")
        envelope = surface.parse_error(exchange.response_body)
        journal.record(
            "probe_error_envelope", envelope.type if envelope else "not in this surface's shape"
        )


def _follow_up(client, surface, model, ingress, budget: int) -> None:
    """A plain request on the same surface after the probe must still be served."""
    body = surface.minimal_body(model.public_id, budget, surface.reply_prompt)
    exchange = client.post(surface.path, json_body=body)
    assert_not_throttled(exchange, ingress)
    status = exchange.transport_error or exchange.status
    journal.record("follow_up_status", status, "200", ok=exchange.status == 200)
    if exchange.status != 200:
        fail("follow_up_status", status, "200 for a plain request after the probe")
    check_basic_completion(exchange, surface, model, ingress)


@pytest.mark.case_id("evolution.unknown_message_field")
def test_unknown_message_field(client, surface, model, ingress):
    _probe(client, surface, model, ingress, "unknown_message_field")


@pytest.mark.case_id("evolution.unknown_block")
def test_unknown_block(client, surface, model, ingress):
    _probe(client, surface, model, ingress, "unknown_block")


@pytest.mark.surface_generic("tools")
@pytest.mark.case_id("evolution.unknown_tool_type")
def test_unknown_tool_type(client, surface, model, ingress):
    _probe(client, surface, model, ingress, "unknown_tool_type")


@pytest.mark.case_id("evolution.unknown_header")
def test_unknown_header(client, surface, model, ingress):
    _probe(client, surface, model, ingress, "unknown_header")


@pytest.mark.surface("messages")
@pytest.mark.case_id("evolution.unknown_beta")
def test_unknown_beta(client, surface, model, ingress):
    _probe(client, surface, model, ingress, "unknown_beta")


@pytest.mark.surface("responses")
@pytest.mark.case_id("evolution.unknown_item")
def test_unknown_item(client, surface, model, ingress):
    _probe(client, surface, model, ingress, "unknown_item")


@pytest.mark.case_id("evolution.unknown_enum_value")
def test_unknown_enum_value(client, surface, model, ingress):
    """A new value of a known setting, as when an API adds a reasoning effort or thinking type."""
    _probe(client, surface, model, ingress, "unknown_enum_value")


@pytest.mark.surface("messages")
@pytest.mark.needs("count_tokens")
@pytest.mark.case_id("evolution.count_tokens_block")
def test_unknown_block_on_token_counting(client, surface, model, ingress):
    """A client counts a conversation before sending it, so a new block type meets it first."""
    probe = "count_tokens_block"
    outcome = _declared(ingress, surface, probe)
    body = variants.count_tokens_probe(surface, model.public_id, DEFAULT_PROMPT)
    journal.record("forward_compat_probe", probe, f"declared {outcome}")
    exchange = client.post(surface.count_tokens_path, json_body=body)
    _judge(exchange, surface, ingress, probe, outcome)
    if _served(exchange, outcome):
        assert_messages_token_count(check_json_response(exchange, ingress))

    plain = surface.count_tokens_body(model.public_id, DEFAULT_PROMPT)
    exchange = client.post(surface.count_tokens_path, json_body=plain)
    assert_not_throttled(exchange, ingress)
    status = exchange.transport_error or exchange.status
    journal.record("follow_up_status", status, "200", ok=exchange.status == 200)
    if exchange.status != 200:
        fail("follow_up_status", status, "200 for a plain count after the probe")
    assert_messages_token_count(check_json_response(exchange, ingress))
