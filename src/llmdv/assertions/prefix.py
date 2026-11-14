"""Token-prefix stability: no cache-hit or router-scoring inference."""

from __future__ import annotations

from ..evidence import journal


def common_prefix_length(left: list[int], right: list[int]) -> int:
    for index, (a, b) in enumerate(zip(left, right, strict=False)):
        if a != b:
            return index
    return min(len(left), len(right))


def assert_prefix_pair(
    left: list[int], repeated: list[int], right: list[int], minimum: int
) -> None:
    stable = left == repeated
    journal.record("prefix_control_repeatable", stable, True, ok=stable)
    assert stable, "identical fixture inputs render differently"
    changed = left != right
    journal.record("prefix_variant_changes_tokens", changed, True, ok=changed)
    assert changed, "changed fixture input produced identical tokens; input may be ignored"
    common = common_prefix_length(left, right)
    ok = common >= minimum
    journal.record(
        "stable_prefix_tokens",
        {"common": common, "left": len(left), "right": len(right)},
        f"at least {minimum} shared initial tokens",
        ok=ok,
    )
    assert ok, (
        f"volatile input moved into reusable prefix: {common} shared tokens, required {minimum}"
    )
