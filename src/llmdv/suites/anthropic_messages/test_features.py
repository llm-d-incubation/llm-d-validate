"""Named Messages features: served as the API defines them, or refused cleanly, as declared.

A fleet that cannot serve a feature has two honest answers: accept it and ignore it where
nothing observable depends on it, or refuse it with a 4xx in the native envelope. A 5xx, a
refusal no SDK can parse, or a 200 whose answer never saw the content are the failures.
"""

import pytest

from ...assertions.anthropic import STOP_REASONS, assert_clean_rejection, assert_prefill_continued
from ...assertions.context import assert_marker_followed
from ...assertions.messages import assert_messages_token_count
from ...evidence import journal
from ...profile.schema import MESSAGES_FEATURES
from ..conftest import check_basic_completion, check_identity, check_json_response
from .features import (
    COUNTED,
    FEATURES,
    PREFILL,
    UNREACHABLE_FIXTURE,
    count_body,
    new_marker,
)

pytestmark = pytest.mark.surface("messages")


def _declared(ingress, feature: str) -> str:
    outcome = ingress.capabilities.messages_features.get(feature)
    if outcome is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no outcome for Messages feature "
            f"{feature!r}"
        )
    journal.record("messages_feature", {"feature": feature, "declared": outcome})
    return outcome


def _fixture(ingress, name: str) -> str:
    """The declared instrument a feature's request needs, or a classified skip without it."""
    value = getattr(ingress.capabilities, name)
    if value is None:
        pytest.skip(
            f"[requires_instrumentation] ingress {ingress.id!r} declares no {name}: the feature "
            "needs a hosted fixture the deployment can fetch"
        )
    journal.record(name, value)
    return value


def _refusal_fixture(ingress, name: str) -> str:
    """What a declared refusal is sent: the declared instrument when there is one, so a 4xx
    proves a policy and not a failed fetch; else an unreachable URL, as any URL proves it."""
    value = getattr(ingress.capabilities, name) or UNREACHABLE_FIXTURE
    journal.record(name, value)
    return value


def _check_served(check: str, exchange, surface, model, ingress, marker: str) -> None:
    if check in ("answer", "valid", "prefill"):
        completion = check_basic_completion(exchange, surface, model, ingress)
        if check == "answer":
            assert_marker_followed("feature_content_reached_model", completion, marker)
        elif check == "prefill":
            assert_prefill_continued(completion, marker, PREFILL)
        return
    completion = surface.parse_completion(check_json_response(exchange, ingress))
    check_identity(completion, surface, model)
    usage = completion.raw.get("usage") or {}
    if check == "prewarm":
        observed = {
            "content": completion.raw.get("content"),
            "stop_reason": completion.finish_reason,
            "output_tokens": usage.get("output_tokens"),
        }
        ok = observed == {"content": [], "stop_reason": "max_tokens", "output_tokens": 0}
        journal.record(
            "cache_prewarm", observed, "empty content, stop_reason max_tokens, 0 output", ok=ok
        )
        assert ok, "max_tokens 0 must pre-warm without generating"
        return
    reason = completion.finish_reason
    ok = reason in STOP_REASONS - {"tool_use", "pause_turn", "refusal"}
    journal.record("window_stop_reason", reason, "a natural, length or window stop", ok=ok)
    assert ok, f"a generation allowed the whole window ended with {reason!r}"


@pytest.mark.case_id("anthropic_messages.request_feature")
@pytest.mark.parametrize("feature", MESSAGES_FEATURES)
def test_request_feature(client, surface, model, ingress, feature):
    outcome = _declared(ingress, feature)
    spec = FEATURES[feature]
    if spec.check == "window" and not model.max_model_len:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no model.max_model_len")
    marker = new_marker()
    if spec.fixture is not None:
        # Only a served feature needs the instrument; a declared refusal runs without one.
        if outcome == "rejected":
            marker = _refusal_fixture(ingress, spec.fixture)
        else:
            marker = _fixture(ingress, spec.fixture)
    request = spec.build(surface, model, marker)
    exchange = client.post(surface.path, json_body=request.body, headers=request.headers)
    if outcome == "rejected":
        assert_clean_rejection(exchange, surface, ingress)
        return
    _check_served(spec.check, exchange, surface, model, ingress, marker)


@pytest.mark.needs("count_tokens")
@pytest.mark.case_id("anthropic_messages.count_tokens_feature")
@pytest.mark.parametrize("feature", COUNTED)
def test_token_count_feature(client, surface, model, ingress, feature):
    """Token counting takes the same input as generation, so it serves or refuses alike."""
    outcome = _declared(ingress, feature)
    spec = FEATURES[feature]
    request = spec.build(surface, model, new_marker())
    exchange = client.post(
        surface.count_tokens_path, json_body=count_body(request), headers=request.headers
    )
    if outcome == "rejected":
        assert_clean_rejection(exchange, surface, ingress)
        return
    counted = assert_messages_token_count(check_json_response(exchange, ingress))
    baseline_exchange = client.post(
        surface.count_tokens_path, json_body=surface.count_tokens_body(model.public_id, spec.prompt)
    )
    baseline = assert_messages_token_count(check_json_response(baseline_exchange, ingress))
    ok = counted > baseline
    journal.record(
        "feature_counted",
        {"with_feature": counted, "prompt_alone": baseline},
        "more input tokens with the feature's content",
        ok=ok,
    )
    assert ok, f"count_tokens did not count the {feature} content ({baseline} -> {counted})"
