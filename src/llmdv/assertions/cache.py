"""Per-request cache reads: native counts, reuse ratios and whole-block floors."""

from __future__ import annotations

from ..evidence import journal
from ..surfaces.cache import CacheUsage


def cache_counts(usage: CacheUsage) -> tuple[int, int]:
    """Validated (read, input) tokens, with native denominator semantics."""
    read, total = usage.read_tokens, usage.input_tokens
    ok = type(read) is int and type(total) is int and 0 <= read <= total and total > 0
    journal.record(
        "cache_usage",
        {"read_tokens": read, "input_tokens": total},
        "integer counts: 0 <= read <= input, input > 0",
        ok=ok,
    )
    assert ok, "missing or inconsistent native cache usage"
    return read, total


def cache_ratio(usage: CacheUsage) -> float:
    read, total = cache_counts(usage)
    return read / total


def block_floor(prompt_tokens: int, block_size: int, withheld_blocks: int = 0) -> int:
    """Every whole block before the last prompt token, which the engine always recomputes.

    `withheld_blocks` lowers it by the trailing blocks of a hit the deployment declares it
    withholds, as an engine with a speculative-decoding draft recomputes the last chunk of a hit
    for the draft's KV. It never goes below zero.
    """
    return max((prompt_tokens - 1) // block_size - withheld_blocks, 0) * block_size


def withheld_text(withheld_blocks: int) -> str:
    """The declared allowance, for a check's expected value and its failure message."""
    if not withheld_blocks:
        return ""
    plural = "s" if withheld_blocks != 1 else ""
    return f", less {withheld_blocks} trailing block{plural} the deployment declares withheld"


def assert_block_floor(
    label: str, read: int, prompt_tokens: int, block_size: int, withheld_blocks: int = 0
) -> None:
    """A repeat of a `prompt_tokens` prompt reads at least its whole-block floor.

    A declared `withheld_blocks` lowers the floor and never excuses a read of nothing: when the
    lowered floor is zero the prompt is too short to judge, which fails like an empty floor.
    Reading more, up to the full floor, passes, as a correct stack without the draft does.
    """
    floor = block_floor(prompt_tokens, block_size, withheld_blocks)
    allowance = withheld_text(withheld_blocks)
    ok = floor > 0 and read >= floor
    journal.record(
        "cache_block_floor",
        {"request": label, "read": read, "prompt_tokens": prompt_tokens, "block_size": block_size},
        f"read >= {floor}{allowance}",
        ok=ok,
    )
    assert floor > 0, f"a {prompt_tokens}-token prompt fills no {block_size}-token block{allowance}"
    assert ok, (
        f"{label} read {read} cached tokens, below the {floor}-token floor of a "
        f"{prompt_tokens}-token prompt in {block_size}-token blocks{allowance}"
    )


def assert_cold(label: str, read: int) -> None:
    """A request with nothing it may reuse reads exactly zero tokens from cache."""
    journal.record("cache_cold", {"request": label, "read": read}, "0", ok=read == 0)
    assert read == 0, f"{label} read {read} cached tokens, expected none"
