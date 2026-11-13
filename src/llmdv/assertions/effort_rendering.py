"""The reasoning effort the model is actually given, read back from its rendered prompt.

A deployment maps requested efforts onto the ones the template knows, and nothing a client can
call reports the result. vLLM's `prompt_logprobs: 0` returns, for every prompt position after
the first, the prompt token itself with its `decoded_token`, so the rendered prompt can be
rebuilt and the template's effort marker read out of it.
"""

from __future__ import annotations

import re
from typing import Any

from ..evidence import journal
from ..profile.schema import EffortRendering
from . import fail


def rendered_prompt(body: dict[str, Any]) -> str:
    """The prompt text rebuilt from `prompt_logprobs`, or a failure naming what came back.

    Position 0 is null by contract: no token precedes it, so its text is never recovered. A
    template's marker sits after the first token, so that loss does not matter here.
    """
    entries = body.get("prompt_logprobs")
    if not isinstance(entries, list) or len(entries) < 2:
        observed = "absent" if entries is None else f"{type(entries).__name__} {entries!r:.80}"
        fail("prompt_logprobs", observed, "one entry per prompt token")
    pieces: list[str] = []
    for index, entry in enumerate(entries[1:], start=1):
        candidates = list(entry.values()) if isinstance(entry, dict) else []
        token = candidates[0] if candidates else None
        decoded = token.get("decoded_token") if isinstance(token, dict) else None
        if not isinstance(decoded, str):
            fail("prompt_logprobs", f"position {index}: {entry!r:.80}", "a decoded_token")
        pieces.append(decoded)
    journal.record("prompt_logprobs", len(entries), "one entry per prompt token", ok=True)
    return "".join(pieces)


def assert_effort_rendered(prompt: str, config: EffortRendering, effort: str) -> None:
    """The marker the template rendered must be the one declared for the requested effort."""
    expected = config.efforts[effort]
    observed = re.findall(config.pattern, prompt)
    wanted = [] if expected is None else [expected]
    description = "no effort marker" if expected is None else f"marker {expected!r}, once"
    if observed != wanted:
        fail("rendered_effort", {"markers": observed, "prompt": prompt[:400]}, description)
    journal.record("rendered_effort", observed, description, ok=True)
