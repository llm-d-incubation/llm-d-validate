"""The unrendered-fields case, proven to fail on a layer that places requests by them."""

import json

import pytest

from tests.test_cache_repeats import SURFACES
from tests.test_cache_reuse import BLOCKS, RATIO, run_cache_case

SELECT = "test_unrendered_fields_reuse"


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (BLOCKS, None, "pass"),
        (RATIO, None, "pass"),
        (BLOCKS, "fields_keyed", "fail"),
        (RATIO, "fields_keyed", "fail"),
    ],
)
def test_unrendered_fields_keep_reuse(tmp_path, surface, declaration, defect, outcome):
    code, records = run_cache_case(tmp_path, surface, declaration, SELECT, defect)
    assert code == (1 if outcome == "fail" else 0), records
    assert {r["variant"].rpartition("/")[2]: r["outcome"] for r in records} == {
        "sampling": outcome,
        "identity": outcome,
    }, records


@pytest.mark.parametrize(
    "surface,field", [("chat_completions", "user"), ("responses", "user"), ("messages", "metadata")]
)
def test_identity_uses_each_surface_field(tmp_path, surface, field):
    code, _ = run_cache_case(tmp_path, surface, BLOCKS, f"{SELECT} and identity")
    assert code == 0
    bodies = [
        json.loads(line)["request_body"]
        for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    first, second = (body[field] for body in bodies)
    assert first != second


@pytest.mark.parametrize("surface", SURFACES)
def test_repeat_case_misses_a_fields_keyed_layer(tmp_path, surface):
    """The repeat case sends the same fields twice, so a layer keyed by them stays warm."""
    code, records = run_cache_case(
        tmp_path, surface, BLOCKS, "test_native_cache_reuse", "fields_keyed"
    )
    assert code == 0, records
    assert [r["outcome"] for r in records] == ["pass"], records
