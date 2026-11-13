"""Token counting against fleets that leave the system prompt or tools out of the count."""

from __future__ import annotations

import pytest

from tests.test_messages_features import by_variant, failed, fake, run, sent

CASE = "anthropic_messages.count_tokens_parts"


@pytest.mark.parametrize(
    "defect,rejects,failing",
    [
        (None, (), set()),
        ("count_ignores_system", (), {"system"}),
        ("count_ignores_tools", (), {"tools"}),
        (None, ("thinking_config",), {"thinking_config"}),
        (None, ("thinking_block",), {"prior_thinking"}),
    ],
)
def test_every_part_of_the_input_is_counted(tmp_path, defect, rejects, failing):
    with fake(defect=defect, rejects=rejects) as fleet:
        code, records = run(
            tmp_path,
            fleet,
            "test_token_count_covers_each_part",
            checks=["count_tokens_parts"],
            messages_thinking=["adaptive"],
        )
    variants = by_variant(records, CASE)
    assert set(variants) == {"system", "tools", "thinking_config", "prior_thinking"}
    for part, record in variants.items():
        assert record["outcome"] == ("fail" if part in failing else "pass"), (part, record)
    assert code == (1 if failing else 0)
    if "system" in failing:
        assert failed(variants["system"], "count_tokens_system")
    bodies = [e["request_body"] for e in sent(tmp_path)]
    assert all("max_tokens" not in b for b in bodies)
    assert {"system", "tools", "thinking"} <= {k for b in bodies for k in b}


def test_the_thinking_part_needs_a_declared_mode(tmp_path):
    with fake() as fleet:
        _, records = run(
            tmp_path,
            fleet,
            "test_token_count_covers_each_part and thinking_config",
            checks=["count_tokens_parts"],
        )
    (record,) = [r for r in records if r.get("case_id") == CASE]
    assert record["skip_class"] == "undeclared"
