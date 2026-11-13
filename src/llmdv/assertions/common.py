"""Assertions shared by every surface.

Each one records what it observed into the run's check journal before deciding, so a passing
case leaves proof of what was actually inspected and a failing one leaves the value that caused
it. Without that, a green run is only a claim that nothing raised.

These are deliberately not per-test helpers. `assert_not_aborted` exists because a
rollout probe found that a draining vLLM rejects work through
`_reject_add_in_shutdown`, which arrives as `finish_reason: "abort"` on an HTTP **200** -- so a
suite that checks status alone scores a dropped request as a success. Putting it here means
every surface inherits it rather than each test remembering.
"""

from __future__ import annotations

import re

from ..evidence import journal
from ..profile.schema import Model
from ..surfaces.base import Completion

# Terminal reasons that mean the engine refused or abandoned the request, not that it finished.
ABORTED_REASONS = frozenset({"abort", "aborted", "cancelled", "canceled"})

LENGTH_REASONS = frozenset({"length", "max_tokens"})

# Room beside the expected answer for punctuation, emphasis or a short lead-in such as "The
# answer is". A reasoning trace that reaches the answer channel runs to hundreds of characters.
ANSWER_ONLY_SLACK_CHARS = 32

_MARKUP = re.compile(r"</?\s*([A-Za-z][\w-]*)[^<>]*>")

HEALTHY_FINISH_REASONS = frozenset(
    {
        "stop",
        "stop_sequence",
        "length",
        "max_tokens",
        "end_turn",
        "tool_calls",
        "tool_use",
        "content_filter",
    }
)


def assert_not_aborted(completion: Completion) -> None:
    reason = (completion.finish_reason or "").lower()
    aborted = reason in ABORTED_REASONS
    journal.record("finish_reason", completion.finish_reason, "not an abort", ok=not aborted)
    assert not aborted, (
        f"finish_reason={completion.finish_reason!r} on an otherwise successful response: the "
        f"engine refused or abandoned this request. A draining replica reports this on HTTP 200, "
        f"so status alone would have scored it as a success."
    )


def assert_healthy_finish(completion: Completion) -> None:
    assert_not_aborted(completion)
    reason = (completion.finish_reason or "").lower()
    recognised = reason in HEALTHY_FINISH_REASONS
    journal.record(
        "finish_reason_recognised",
        completion.finish_reason,
        f"one of {sorted(HEALTHY_FINISH_REASONS)}",
        ok=recognised,
    )
    assert recognised, (
        f"finish_reason={completion.finish_reason!r} is not a recognised terminal reason "
        f"(expected one of: {', '.join(sorted(HEALTHY_FINISH_REASONS))})"
    )


def assert_has_output(completion: Completion, model: Model) -> None:
    """Assert the model generated something, counting reasoning as output.

    Reasoning counts on purpose. On a fleet served without `--reasoning-parser` the whole token
    budget can go into a `<think>` block, leaving
    `content` empty on a perfectly healthy response. Asserting non-empty content there fails a
    working stack.
    """
    content_chars = len(completion.text.strip())
    reasoning_chars = len(completion.reasoning_text.strip())
    journal.record(
        "output_chars",
        f"content={content_chars} reasoning={reasoning_chars} tools={len(completion.tool_calls)}",
        "text, reasoning or a tool call",
        ok=completion.has_output,
    )
    if completion.has_output:
        return
    detail = (
        f"reasoning arrives inline as <{model.reasoning.inline_tag}> on this fleet"
        if model.reasoning.is_inline
        else f"reasoning field is {model.reasoning.field_name!r}"
        if model.reasoning.is_separate_field
        else "this fleet declares no reasoning mode"
    )
    raise AssertionError(
        f"response carried no content and no reasoning text ({detail}); "
        f"finish_reason={completion.finish_reason!r}"
    )


def assert_has_answer(completion: Completion, max_tokens: int) -> None:
    """Require usable answer text at an explicitly declared output budget.

    This is stronger than assert_has_output and belongs only in the opt-in budget cases.
    A length-limited response with an answer is allowed; reasoning alone is not an answer.
    """
    answer_chars = len(completion.text.strip())
    reasoning_chars = len(completion.reasoning_text.strip())
    output_tokens = completion.usage.completion_tokens
    within_budget = type(output_tokens) is int and 0 <= output_tokens <= max_tokens
    journal.record(
        "answer_budget",
        {
            "max_tokens": max_tokens,
            "answer_chars": answer_chars,
            "reasoning_chars": reasoning_chars,
            "output_tokens": output_tokens,
            "finish_reason": completion.finish_reason,
        },
        "non-empty answer with output_tokens within the declared budget",
        ok=answer_chars > 0 and within_budget,
    )
    if answer_chars:
        assert within_budget, (
            f"output_tokens={output_tokens!r} is not an integer within 0..{max_tokens}; "
            "an answer beyond the declared output budget does not prove sufficiency"
        )
        return
    exhausted = (completion.finish_reason or "").lower() in LENGTH_REASONS and reasoning_chars > 0
    cause = "output budget exhausted before an answer" if exhausted else "no answer text returned"
    raise AssertionError(
        f"{cause}: max_tokens={max_tokens}, finish_reason={completion.finish_reason!r}, "
        f"answer_chars={answer_chars}, reasoning_chars={reasoning_chars}"
    )


def assert_answer_only(completion: Completion, model: Model, expected: str) -> None:
    """Require the answer channel to carry the expected answer and none of the reasoning.

    Opt-in, like assert_has_answer: only cases whose prompt has one short expected answer call
    it. Punctuation, emphasis, a short lead-in, or a value the reasoning also reached are still
    the answer alone. Leaked reasoning is reasoning markup left in the answer text, reasoning
    that arrived inline where the profile declares a separate channel for it, or answer text
    too long to be the answer. An empty answer at a length limit is budget exhaustion, which
    assert_has_answer judges; at a natural finish the answer is missing.
    """
    answer = completion.text.strip()
    declared = model.reasoning
    tags = {"think", "thinking", (declared.inline_tag or "think").lower()}
    markup = sorted({m.group(0) for m in _MARKUP.finditer(answer) if m.group(1).lower() in tags})
    inline_declared = declared.is_inline and not declared.is_separate_field
    exhausted = not answer and (completion.finish_reason or "").lower() in LENGTH_REASONS
    limit = len(expected) + ANSWER_ONLY_SLACK_CHARS
    cause = None
    if completion.reasoning_source == "inline" and not inline_declared:
        channel = (
            f"reasoning field {declared.field_name!r}"
            if declared.is_separate_field
            else "no inline reasoning"
        )
        cause = (
            f"reasoning arrived inline as <{completion.reasoning_carrier}> in the answer "
            f"channel, but the profile declares {channel}"
        )
    elif markup:
        cause = f"reasoning markup {markup} left in the answer text"
    elif not answer and not exhausted:
        cause = "no answer text at a natural finish; the answer went missing or into reasoning"
    elif answer and expected.lower() not in answer.lower():
        cause = f"the answer text does not contain the expected answer {expected!r}"
    elif len(answer) > limit:
        cause = f"the answer text is {len(answer)} characters, over the {limit} the answer needs"
    journal.record(
        "answer_only",
        {
            "expected": expected,
            "answer_chars": len(answer),
            "answer_head": answer[:80],
            "reasoning_chars": len(completion.reasoning_text.strip()),
            "reasoning_source": completion.reasoning_source,
            "finish_reason": completion.finish_reason,
            "judged": not exhausted,
        },
        f"{expected!r} alone within {limit} characters, no reasoning in the answer channel; "
        "not judged when the budget ran out before an answer",
        ok=cause is None,
    )
    assert cause is None, f"answer channel does not carry the answer alone: {cause}"


def assert_usage_present_and_consistent(completion: Completion) -> None:
    usage = completion.usage
    present = usage.prompt_tokens is not None and usage.completion_tokens is not None
    journal.record(
        "usage",
        f"{usage.prompt_tokens}+{usage.completion_tokens}={usage.total_tokens}",
        "present and self-consistent",
        ok=present and usage.is_self_consistent,
    )
    assert usage.prompt_tokens is not None, "usage.prompt_tokens missing"
    assert usage.completion_tokens is not None, "usage.completion_tokens missing"
    assert usage.is_self_consistent, (
        f"usage does not add up: prompt={usage.prompt_tokens} + "
        f"completion={usage.completion_tokens} != total={usage.total_tokens}"
    )
