"""Observable effects of declared vendor options, never mere HTTP acceptance."""

from __future__ import annotations

from typing import Any

from ..evidence import journal
from ..surfaces.base import Usage
from . import fail


def assert_token_ids(body: dict[str, Any], usage: Usage) -> None:
    choices = body.get("choices")
    choice = (
        choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    )
    for name, values, count in (
        ("prompt_token_ids", body.get("prompt_token_ids"), usage.prompt_tokens),
        ("token_ids", choice.get("token_ids"), usage.completion_tokens),
    ):
        if not (
            isinstance(values, list)
            and bool(values)
            and (len(values) == count)
            and all(type(value) is int and value >= 0 for value in values)
        ):
            fail(name, values, f"{count} non-negative integer IDs")
        journal.record(name, values, f"{count} non-negative integer IDs", ok=True)
