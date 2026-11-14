"""Stop sequences and token limits against fleets that ignore them or misreport them."""

from __future__ import annotations

import pytest

from tests.fakes import FakeFleet
from tests.test_messages_features import CONTEXT, by_variant, failed, fake, run, sent

STOP = "anthropic_messages.stop_sequence"
LIMIT = "anthropic_messages.max_tokens_stop"


@pytest.mark.parametrize(
    "defect,passes",
    [
        (None, True),
        ("ignores_stop_sequence", False),
        ("stop_reason_only", False),
        ("no_stop_sequence_field", False),
    ],
)
def test_stop_sequence_is_honoured_and_named(tmp_path, defect, passes):
    with fake(defect=defect) as fleet:
        code, records = run(tmp_path, fleet, "test_stop_sequence_ends", checks=["stop_sequences"])
    variants = by_variant(records, STOP)
    assert set(variants) == {"message", "stream"}
    for mode, record in variants.items():
        assert record["outcome"] == ("pass" if passes else "fail"), (mode, record)
        assert failed(record, "stop_sequence") is not passes
    assert code == (0 if passes else 1)
    for exchange in sent(tmp_path):
        assert exchange["request_body"]["stop_sequences"] == [", 7"]


@pytest.mark.parametrize(
    "fleet_factory,passes",
    [
        (lambda: fake(), True),
        (lambda: fake(defect="overruns_max_tokens"), False),
        (lambda: FakeFleet(max_model_len=CONTEXT), False),
    ],
    ids=["faithful", "overrun", "unaware"],
)
def test_max_tokens_one_stops_at_the_limit(tmp_path, fleet_factory, passes):
    with fleet_factory() as fleet:
        code, records = run(
            tmp_path, fleet, "test_max_tokens_one_stops", checks=["max_tokens_limit"]
        )
    (record,) = [r for r in records if r.get("case_id") == LIMIT]
    assert record["outcome"] == ("pass" if passes else "fail"), record
    assert failed(record, "max_tokens_stop") is not passes
    (exchange,) = sent(tmp_path)
    assert exchange["request_body"]["max_tokens"] == 1


def test_stop_cases_are_opt_in(tmp_path):
    with fake() as fleet:
        code, records = run(tmp_path, fleet, "test_stop_sequence_ends or test_max_tokens_one")
        assert not [s for s in fleet.seen if s[0] == "POST"]
    assert code == 0 and {r["skip_class"] for r in records} == {"undeclared"}
