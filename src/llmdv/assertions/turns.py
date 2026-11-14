"""Reuse between requests that share a prefix without being identical: turns of one session."""

from __future__ import annotations

from ..evidence import journal
from .cache import block_floor, withheld_text


def turn_floor(
    previous_prompt: int, block_size: int, slack_blocks: int = 1, withheld_blocks: int = 0
) -> int:
    """The whole blocks of an earlier prompt a later one must read, less `slack_blocks`.

    An earlier prompt ends with the template's opening of the assistant turn, which may render
    differently once a later request carries the reply, so its last whole block is not required.

    `withheld_blocks` is subtracted on top of the slack, not absorbed by it. The slack bounds
    where the later prompt's hit can end; a deployment that withholds the trailing blocks of a
    hit then reads that many fewer than the hit, wherever it ended.
    """
    return block_floor(previous_prompt, block_size, slack_blocks + withheld_blocks)


def assert_turn_floor(
    label: str,
    read: int,
    previous_prompt: int,
    block_size: int,
    slack_blocks: int = 1,
    withheld_blocks: int = 0,
) -> None:
    """A request that extends an earlier prompt reads that prompt's whole blocks."""
    floor = turn_floor(previous_prompt, block_size, slack_blocks, withheld_blocks)
    allowance = withheld_text(withheld_blocks)
    ok = floor > 0 and read >= floor
    journal.record(
        "cache_turn_floor",
        {
            "request": label,
            "read": read,
            "previous_prompt_tokens": previous_prompt,
            "block_size": block_size,
            "slack_blocks": slack_blocks,
        },
        f"read >= {floor}{allowance}",
        ok=ok,
    )
    assert floor > 0, (
        f"a {previous_prompt}-token earlier prompt fills too few {block_size}-token blocks to judge"
        f"{allowance}"
    )
    assert ok, (
        f"{label} read {read} cached tokens, below the {floor}-token floor of the earlier "
        f"{previous_prompt}-token prompt in {block_size}-token blocks{allowance}"
    )


def assert_turn_ratio(label: str, read: int, previous_prompt: int, min_ratio: float) -> None:
    """Without a declared block size, the share of the earlier prompt read from cache."""
    ratio = read / previous_prompt if previous_prompt > 0 else 0.0
    ok = ratio >= min_ratio
    journal.record(
        "cache_turn_ratio",
        {"request": label, "read": read, "previous_prompt_tokens": previous_prompt},
        f"read / previous prompt >= {min_ratio}",
        ok=ok,
    )
    assert ok, f"{label} read {ratio:.3f} of the earlier prompt, below declared {min_ratio:.3f}"
