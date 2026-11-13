"""Check the reasoning origin preserved by the Chat surface."""

from ..evidence import journal
from ..profile.schema import Model
from ..surfaces.base import Completion
from . import fail


def assert_chat_reasoning(completion: Completion, model: Model, *, enabled: bool = True) -> None:
    config = model.reasoning
    observed = {
        "source": completion.reasoning_source,
        "carrier": completion.reasoning_carrier,
        "reasoning_chars": len(completion.reasoning_text),
    }
    expected = ("field", config.field_name) if config.field_name else ("inline", config.inline_tag)
    valid = (
        bool(completion.reasoning_text.strip())
        and expected[1] is not None
        and (completion.reasoning_source, completion.reasoning_carrier) == expected
    )
    if not enabled:
        expected = "no reasoning for reasoning_effort=none"
        valid = not completion.reasoning_text.strip()
    if not valid:
        fail("chat_reasoning_shape", observed, expected)
    journal.record("chat_reasoning_shape", observed, expected, ok=True)
