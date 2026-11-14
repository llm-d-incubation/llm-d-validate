"""Per-token logprob counts: one entry per completion token, each with the requested width."""

from __future__ import annotations

import math
from typing import Any

from ..evidence import journal
from . import fail


def _valid(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("token"), str)
        and type(entry.get("logprob")) in (int, float)
        and math.isfinite(entry["logprob"])
        and entry["logprob"] <= 0
    )


def assert_token_entries(entries: Any, count: Any, width: int) -> None:
    """Chat logprobs: exactly `count` valid entries, each with exactly `width` alternatives."""
    size = len(entries) if isinstance(entries, list) else entries
    if not (isinstance(entries, list) and type(count) is int and len(entries) == count):
        fail("logprob_entries", {"entries": size, "completion_tokens": count}, "equal counts")
    widths = set()
    for entry in entries:
        top = entry.get("top_logprobs") if isinstance(entry, dict) else None
        if not (_valid(entry) and isinstance(top, list) and all(_valid(t) for t in top)):
            fail("logprob_entry", entry, "a token, a finite non-positive logprob, alternatives")
        widths.add(len(top))
    if widths - {width}:
        fail("top_logprobs_width", sorted(widths), f"exactly {width} alternatives per token")
    journal.record(
        "logprob_entries",
        {"entries": count, "width": width},
        "one entry per completion token, exact width",
        ok=True,
    )


def assert_text_logprobs(
    logprobs: Any, count: Any, width: int, *, first_unscored: bool = False
) -> None:
    """Completions logprobs: aligned per-token arrays and `width` or `width + 1` alternatives.

    The engine adds the sampled token to its alternatives when it is not among the top ones,
    which is why one more than requested is valid. With `echo` the arrays start at the first
    prompt token, which nothing precedes, so its logprob and alternatives are null.
    """
    if not isinstance(logprobs, dict):
        fail("text_logprobs", logprobs, "an object")
    keys = ("tokens", "token_logprobs", "top_logprobs", "text_offset")
    lengths = {key: len(logprobs.get(key) or []) for key in keys}
    if set(lengths.values()) != {count}:
        fail("text_logprobs_lengths", lengths, f"{count} entries in each array")
    tokens = logprobs["tokens"]
    if not (isinstance(tokens, list) and all(isinstance(token, str) for token in tokens)):
        fail("text_logprob_tokens", tokens, "string tokens")
    values = logprobs["token_logprobs"]
    tops = logprobs["top_logprobs"]
    if first_unscored:
        if values[:1] != [None] or tops[:1] != [None]:
            fail(
                "first_prompt_token", [values[:1], tops[:1]], "a null logprob and null alternatives"
            )
        values, tops = values[1:], tops[1:]
    if not all(type(v) in (int, float) and math.isfinite(v) and v <= 0 for v in values):
        fail("token_logprobs", values, "finite non-positive logprobs")
    valid_tops = all(
        isinstance(top, dict)
        and all(
            type(value) in (int, float) and math.isfinite(value) and value <= 0
            for value in top.values()
        )
        for top in tops
    )
    if not valid_tops:
        fail("top_logprobs", tops, "alternatives with finite non-positive logprobs")
    widths = [len(top) if isinstance(top, dict) else -1 for top in tops]
    if not all(width <= w <= width + 1 for w in widths):
        fail("top_logprobs_width", widths, f"{width} or {width + 1} alternatives per token")
    offsets = logprobs["text_offset"]
    if not (all(type(o) is int for o in offsets) and offsets == sorted(offsets)):
        fail("text_offset", offsets, "non-decreasing integer offsets")
    journal.record("text_logprobs", lengths, "aligned per-token arrays", ok=True)
