"""Validate every requested logprob, with one bounded summary per request."""

import math
from typing import Any

from ..evidence import journal
from . import fail


def _valid_token(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("token"), str)
        and type(entry.get("logprob")) in (int, float)
        and math.isfinite(entry["logprob"])
        and entry["logprob"] <= 0
    )


def assert_logprobs(parts: list[Any], maximum: int) -> None:
    tokens = alternatives = 0
    low, high = math.inf, -math.inf
    for entries in parts:
        if not isinstance(entries, list) or not entries:
            fail("output_logprobs", entries, "non-empty token logprobs in every text part")
        for entry in entries:
            if not _valid_token(entry):
                fail("output_logprobs", entry, "a text token and finite non-positive logprob")
            top = entry.get("top_logprobs")
            if not (
                isinstance(top, list)
                and 0 < len(top) <= maximum
                and all(_valid_token(item) for item in top)
            ):
                fail("output_logprobs", entry, f"1..{maximum} valid alternatives")
            tokens += 1
            alternatives += len(top)
            values = [entry["logprob"], *(item["logprob"] for item in top)]
            low, high = min(low, *values), max(high, *values)
    if not tokens:
        fail("output_logprobs", parts, "at least one output text token")
    journal.record(
        "output_logprobs",
        {"tokens": tokens, "alternatives": alternatives, "minimum": low, "maximum": high},
        "valid requested logprobs",
        ok=True,
    )
