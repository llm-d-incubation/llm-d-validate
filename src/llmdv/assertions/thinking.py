"""The native Messages thinking contract, including redacted thinking blocks."""

from __future__ import annotations

from ..evidence import journal
from ..surfaces.base import Completion
from . import fail


def assert_thinking_blocks(completion: Completion, mode: str) -> None:
    blocks = completion.raw.get("content")
    valid = isinstance(blocks, list) and all(isinstance(b, dict) for b in blocks)
    thinking = (
        [
            b
            for b in blocks
            if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
        ]
        if isinstance(blocks, list)
        else []
    )
    for block in thinking:
        fields = ("thinking", "signature") if block["type"] == "thinking" else ("data",)
        valid = valid and all(isinstance(block.get(f), str) and block[f].strip() for f in fields)
    if mode == "enabled":
        valid = valid and bool(thinking) and blocks[0] is thinking[0]
    elif mode == "disabled":
        valid = valid and not thinking and not completion.reasoning_text.strip()
    if not valid:
        fail("messages_thinking", blocks, f"well-formed thinking blocks for mode {mode!r}")
    observed = {
        "mode": mode,
        "thinking_blocks": len(thinking),
        "types": [b["type"] for b in thinking],
        "characters": sum(len(v) for b in thinking for v in b.values() if isinstance(v, str)),
    }
    journal.record(
        "messages_thinking", observed, "well-formed blocks matching requested mode", ok=bool(valid)
    )
