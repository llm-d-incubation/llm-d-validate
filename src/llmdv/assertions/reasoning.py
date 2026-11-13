"""Native reasoning usage is optional unless the ingress promises to report it."""

from ..evidence import journal
from ..surfaces.base import Completion


def assert_reasoning_usage(completion: Completion) -> None:
    usage = completion.usage
    count, output = usage.reasoning_tokens, usage.completion_tokens
    ok = (
        type(count) is int
        and type(output) is int
        and 0 <= count <= output
        and (not completion.reasoning_text.strip() or count > 0)
    )
    observed = {
        "reasoning_tokens": count,
        "output_tokens": output,
        "reasoning_chars": len(completion.reasoning_text),
    }
    journal.record(
        "reasoning_usage",
        observed,
        "integer within output tokens; positive when reasoning text is present",
        ok=ok,
    )
    assert ok, f"invalid or missing declared reasoning usage: {observed}"
