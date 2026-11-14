"""Messages error types against fleets that map them, mislabel them or answer in another shape."""

from __future__ import annotations

import pytest

from tests.fakes import FakeFleet
from tests.test_messages_features import CONTEXT, by_variant, failed, fake, run

CASE = "anthropic_messages.error_type"
TRIGGERS = {
    "missing_max_tokens",
    "invalid_thinking_type",
    "thinking_budget_too_small",
    "unknown_model",
    "token_count_unknown_model",
}


def test_a_faithful_fleet_types_every_error(tmp_path):
    with fake() as fleet:
        code, records = run(
            tmp_path, fleet, "test_error_type_matches_status", checks=["error_types"]
        )
    variants = by_variant(records, CASE)
    assert set(variants) == TRIGGERS
    assert code == 0, {v: r["outcome"] for v, r in variants.items()}
    for record in variants.values():
        assert any(c["name"] == "anthropic_error_type" and c["ok"] for c in record["checks"])


@pytest.mark.parametrize(
    "defect,check",
    [("wrong_error_type", "anthropic_error_type"), ("openai_error_shape", "error_envelope")],
)
def test_a_mislabelled_or_foreign_error_fails(tmp_path, defect, check):
    with fake(defect=defect) as fleet:
        code, records = run(
            tmp_path, fleet, "test_error_type_matches_status", checks=["error_types"]
        )
    variants = by_variant(records, CASE)
    assert code == 1
    assert all(r["outcome"] == "fail" and failed(r, check) for r in variants.values()), variants


def test_an_ignored_thinking_type_fails_on_status(tmp_path):
    """A layer that drops an invalid thinking type answers 200 with default thinking."""
    with FakeFleet(transport={"defects": {"thinking_type_ignored"}}) as fleet:
        code, records = run(
            tmp_path,
            fleet,
            "test_error_type_matches_status and invalid_thinking",
            checks=["error_types"],
        )
    (record,) = by_variant(records, CASE).values()
    assert code == 1 and failed(record, "invalid_thinking_type_status")


def test_a_transport_error_type_defect_survives_the_native_backend(tmp_path):
    with FakeFleet(
        max_model_len=CONTEXT,
        anthropic={},
        transport={"defects": {"anthropic_error_type"}},
    ) as fleet:
        code, records = run(
            tmp_path, fleet, "test_error_type_matches_status", checks=["error_types"]
        )
    variants = by_variant(records, CASE)
    assert code == 1
    assert all(failed(record, "anthropic_error_type") for record in variants.values())


@pytest.mark.parametrize("declared", [False, True], ids=["undeclared", "declared"])
def test_a_throttle_does_not_validate_a_thinking_error(tmp_path, declared):
    with FakeFleet(max_model_len=CONTEXT, anthropic={}, throttle=True) as fleet:
        code, records = run(
            tmp_path,
            fleet,
            "test_error_type_matches_status and invalid_thinking_type",
            checks=["error_types"],
            rate_limited=declared,
        )
    (record,) = by_variant(records, CASE).values()
    assert code == (1 if not declared else 0)
    if declared:
        assert record["outcome"] == "skip" and record["skip_class"] == "throttled"
    else:
        assert record["outcome"] == "fail" and failed(record, "throttled_undeclared")


def test_error_types_are_opt_in(tmp_path):
    with fake() as fleet:
        code, records = run(tmp_path, fleet, "test_error_type_matches_status")
        assert not [s for s in fleet.seen if s[0] == "POST"]
    assert code == 0 and {r["skip_class"] for r in records} == {"undeclared"}


def test_a_shared_fleet_gets_no_request_without_max_tokens(tmp_path):
    with fake() as fleet:
        code, records = run(
            tmp_path,
            fleet,
            "test_error_type_matches_status and missing_max_tokens",
            checks=["error_types"],
            fleet_policy="shared",
        )
        assert not [s for s in fleet.seen if s[0] == "POST"]
    (record,) = [r for r in records if r.get("case_id") == CASE]
    assert code == 0 and record["skip_class"] == "excluded_by_policy"
