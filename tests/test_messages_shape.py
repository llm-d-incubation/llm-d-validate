"""The native Message and stream contract against fleets that bend it."""

from __future__ import annotations

import pytest

from tests.fakes import FakeFleet, message_events, named_sse
from tests.test_messages_features import CONTEXT, failed, fake, run

SHAPE = "anthropic_messages.response_shape"
MATCH = "anthropic_messages.stream_matches_message"
SDK = "anthropic_messages.stream_parses_with_anthropic_sdk"


def outcomes(records):
    return {r["case_id"]: r for r in records if r.get("case_id")}


def test_a_faithful_fleet_passes_every_shape_case(tmp_path):
    with fake() as fleet:
        code, records = run(tmp_path, fleet, "test_shape", checks=["response_shape"])
    by_case = outcomes(records)
    assert set(by_case) == {SHAPE, MATCH, SDK}
    assert code == 0, by_case
    assert all(r["outcome"] == "pass" and r["checks"] for r in by_case.values())


@pytest.mark.parametrize(
    "defect,check",
    [
        ("bad_stop_reason", "message_stop_reason"),
        ("no_stop_sequence_field", "message_stop_sequence"),
        ("non_null_stop_sequence", "message_stop_sequence"),
        ("stop_details_on_end_turn", "message_stop_details"),
        ("bad_cache_counters", "usage_cache_creation_sum"),
        ("thinking_tokens_over_output", "usage_thinking_tokens"),
    ],
)
def test_a_bent_message_fails_the_shape_case(tmp_path, defect, check):
    with fake(defect=defect) as fleet:
        code, records = run(
            tmp_path, fleet, "test_message_fields_are_native", checks=["response_shape"]
        )
    record = outcomes(records)[SHAPE]
    assert code == 1 and record["outcome"] == "fail" and failed(record, check), record["checks"]


@pytest.mark.parametrize("defect", ["zero_start_usage", "stream_differs"])
def test_a_stream_that_disagrees_with_its_message_fails(tmp_path, defect):
    with fake(defect=defect) as fleet:
        code, records = run(
            tmp_path, fleet, "test_stream_matches_message", checks=["response_shape"]
        )
    record = outcomes(records)[MATCH]
    assert code == 1 and failed(record, "stream_matches_message"), record["checks"]


def test_a_stream_the_sdk_cannot_accumulate_fails(tmp_path):
    broken = named_sse([e for e in message_events() if e["type"] != "message_start"])
    with FakeFleet(max_model_len=CONTEXT, messages_stream=broken) as fleet:
        code, records = run(
            tmp_path, fleet, "test_stream_accumulates_with_pinned", checks=["response_shape"]
        )
    assert code == 1 and outcomes(records)[SDK]["outcome"] == "fail"


def test_shape_cases_are_opt_in(tmp_path):
    with fake() as fleet:
        code, records = run(tmp_path, fleet, "test_shape")
        assert not [s for s in fleet.seen if s[0] == "POST"]
    assert code == 0 and {r["skip_class"] for r in records} == {"undeclared"}
