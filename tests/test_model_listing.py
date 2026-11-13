"""The model listing reports the declared context length wherever it reports one at all."""

from __future__ import annotations

import pytest

from tests.fakes import MAX_MODEL_LEN, FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile

CASE = "discovery.models_lists_declared_model"


@pytest.mark.parametrize(
    "advertised,declared,outcome,ok",
    [
        (MAX_MODEL_LEN, MAX_MODEL_LEN, "pass", True),
        (MAX_MODEL_LEN * 2, MAX_MODEL_LEN, "fail", False),
        (None, MAX_MODEL_LEN, "pass", True),
        (MAX_MODEL_LEN * 2, "null", "pass", True),
    ],
    ids=["equal", "mismatch", "omitted", "undeclared"],
)
def test_advertised_context_length_matches_the_declared_one(
    tmp_path, advertised, declared, outcome, ok
):
    with FakeFleet(advertised_max_model_len=advertised) as fleet:
        profile = write_profile(tmp_path, fleet, max_model_len=declared)
        _, records = run_suites(profile, tmp_path / "run", "-k", "models_lists")

    record = next(r for r in records if r["case_id"] == CASE)
    assert record["outcome"] == outcome, record
    check = next(c for c in record["checks"] if c["name"] == "advertised_max_model_len")
    reported = {} if advertised is None else {fleet.model: advertised}
    assert (check["observed"], check["ok"]) == (reported, ok)
    if not ok:
        assert f"advertises max_model_len {reported}" in record["reason"]
