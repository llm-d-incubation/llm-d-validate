"""String and block content forms against a fleet that renders them apart."""

from __future__ import annotations

import pytest

from tests.test_messages_features import by_variant, failed, fake, run, sent

CASE = "anthropic_messages.block_form_same_prompt"


@pytest.mark.parametrize("defect,passes", [(None, True), ("renders_blocks_apart", False)])
def test_both_forms_must_render_the_same_prompt(tmp_path, defect, passes):
    with fake(defect=defect) as fleet:
        code, records = run(tmp_path, fleet, "test_string_and_block_forms", checks=["block_forms"])
    variants = by_variant(records, CASE)
    assert set(variants) == {"user", "system"}
    assert code == (0 if passes else 1)
    for part, record in variants.items():
        assert record["outcome"] == ("pass" if passes else "fail")
        assert failed(record, f"{part}_block_form_tokens") is not passes
    bodies = [e["request_body"] for e in sent(tmp_path)]
    users = [type(b["messages"][0]["content"]) for b in bodies if "system" not in b]
    systems = [type(b["system"]) for b in bodies if "system" in b]
    assert (
        sorted(map(str, users)) == sorted(map(str, systems)) == ["<class 'list'>", "<class 'str'>"]
    )
