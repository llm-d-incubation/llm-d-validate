"""What reached the model, judged from the answer or from the size of the rendered prompt.

A layer can drop part of a conversation and still return a well-formed 200. These checks
compare two requests that differ only in the content under test, so they need no access to
the layer and no knowledge of its tokenizer.
"""

from __future__ import annotations

from ..evidence import journal
from ..surfaces.base import Completion, Surface
from . import fail

# Chat templates add a few tokens per message; real disagreement is far larger than this.
COUNT_TOLERANCE = 0.05


def prompt_size(surface: Surface, completion: Completion) -> int:
    """Input tokens for the whole prompt, cached or not, as the ingress reported them."""
    total = surface.parse_cache_usage(completion.raw).input_tokens
    if total is None:
        # No cache counters on this response: the plain input count is then the whole prompt.
        total = completion.usage.prompt_tokens if completion.usage else None
    if type(total) is not int or total <= 0:
        fail("prompt_size", total, "a positive integer input token count")
    return total


def assert_prompt_grows(name: str, without: int, with_content: int, minimum: int) -> None:
    grew = with_content - without
    ok = grew >= minimum
    journal.record(
        name,
        {"without": without, "with": with_content, "grew": grew},
        f"at least {minimum} more input tokens with the content present",
        ok=ok,
    )
    assert ok, (
        f"{name}: the prompt grew by {grew} tokens, expected at least {minimum}; "
        "the content did not reach the model"
    )


def assert_marker_followed(name: str, completion: Completion, marker: str) -> None:
    """The marker is random, so it can only come back if the entry reached the model.

    Reasoning counts: a model may quote the entry there and still decline to repeat it.
    """
    ok = marker in completion.text or marker in completion.reasoning_text
    journal.record(
        name,
        {"text": completion.text, "in_reasoning": marker in completion.reasoning_text},
        f"output containing {marker}",
        ok=ok,
    )
    assert ok, f"{name}: the output does not contain the marker the model was given"


def assert_count_matches_usage(counted: int, used: int) -> None:
    off = abs(counted - used) / used
    ok = off <= COUNT_TOLERANCE
    journal.record(
        "count_tokens_matches_usage",
        {"counted": counted, "generation_input_tokens": used, "relative_error": round(off, 4)},
        f"within {COUNT_TOLERANCE:.0%} of generation usage",
        ok=ok,
    )
    assert ok, (
        f"count_tokens returned {counted} for a prompt the generation reported as {used} tokens"
    )


# Low and high effort on one prompt; a model that honours the option differs by far more.
EFFORT_RATIO = 1.15
# A relay that buffers a stream delivers its deltas within milliseconds of each other. The
# floor rules that out; the share keeps a fast engine with a short answer from failing.
MIN_STREAM_SPREAD_S = 0.02
STREAM_SPREAD_SHARE = 0.1


def assert_effort_effect(low: list[int], high: list[int]) -> None:
    """Compare mean output tokens over the samples taken at each level."""
    mean_low, mean_high = sum(low) / len(low), sum(high) / len(high)
    ok = mean_low > 0 and mean_high >= EFFORT_RATIO * mean_low
    journal.record(
        "effort_effect",
        {"low_output_tokens": low, "high_output_tokens": high},
        f"mean at high effort is at least {EFFORT_RATIO}x the mean at low",
        ok=ok,
    )
    assert ok, (
        f"effort had no clear effect on output size (low mean {mean_low:.0f}, "
        f"high mean {mean_high:.0f})"
    )


def assert_incremental(delta_times: list[float]) -> None:
    """Deltas must be spread over the stream, measured against how long it took to arrive."""
    spread = delta_times[-1] - delta_times[0] if len(delta_times) >= 2 else None
    needed = max(MIN_STREAM_SPREAD_S, STREAM_SPREAD_SHARE * delta_times[-1]) if delta_times else 0
    ok = spread is not None and spread >= needed
    journal.record(
        "stream_delivery",
        {
            "output_deltas": len(delta_times),
            "first_delta_s": round(delta_times[0], 3) if delta_times else None,
            "last_delta_s": round(delta_times[-1], 3) if delta_times else None,
            "spread_s": round(spread, 3) if spread is not None else None,
        },
        f"two or more output deltas spread over at least {STREAM_SPREAD_SHARE:.0%} of the time "
        f"to the last one, and {MIN_STREAM_SPREAD_S}s",
        ok=ok,
    )
    assert ok, "output deltas arrived together: the stream was buffered, or too short to tell"


def assert_same_prompt_size(own: int, reference: int, reference_id: str) -> None:
    off = abs(own - reference) / reference
    ok = off <= COUNT_TOLERANCE
    journal.record(
        "prompt_size_matches_engine",
        {"this_ingress": own, reference_id: reference, "relative_difference": round(off, 4)},
        f"within {COUNT_TOLERANCE:.0%} of the engine ingress",
        ok=ok,
    )
    assert ok, (
        f"the same request is {own} input tokens here and {reference} at {reference_id!r}; "
        "this layer changed what the model receives"
    )
