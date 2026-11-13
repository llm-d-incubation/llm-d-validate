"""The shared assertions, especially the ones encoding a lesson from this repo's history."""

from __future__ import annotations

import pytest

from llmdv.assertions import (
    assert_answer_only,
    assert_has_output,
    assert_healthy_finish,
    assert_not_aborted,
    assert_usage_present_and_consistent,
)
from llmdv.profile.schema import Model, Reasoning
from llmdv.surfaces import get
from llmdv.surfaces.base import Completion, Usage

INLINE_MODEL = Model(public_id="m", reasoning=Reasoning(inline_tag="think", default_on=True))
FIELD_MODEL = Model(
    public_id="m", reasoning=Reasoning(parser="glm45", field_name="reasoning_content")
)


def completion(**kw):
    base = dict(
        role="assistant", text="ok", finish_reason="stop", usage=Usage(1, 2, 3), reasoning_text=""
    )
    base.update(kw)
    return Completion(**base)


def test_abort_on_a_200_is_caught():
    """The rollout-probe lesson: a draining replica reports abort on HTTP 200."""
    with pytest.raises(AssertionError, match="refused or abandoned"):
        assert_not_aborted(completion(finish_reason="abort"))


def test_abort_detection_is_case_insensitive_and_covers_synonyms():
    for reason in ("ABORT", "aborted", "Cancelled", "canceled"):
        with pytest.raises(AssertionError):
            assert_not_aborted(completion(finish_reason=reason))


def test_normal_finish_reasons_pass():
    for reason in ("stop", "length", "max_tokens", "end_turn", "tool_calls"):
        assert_healthy_finish(completion(finish_reason=reason))


def test_unrecognised_finish_reason_fails_with_the_allowed_set():
    with pytest.raises(AssertionError, match="not a recognised terminal reason"):
        assert_healthy_finish(completion(finish_reason="who_knows"))


def test_reasoning_only_output_counts_as_output():
    """Empty content plus reasoning is a healthy response on a no-parser fleet."""
    assert_has_output(completion(text="", reasoning_text="thinking hard"), INLINE_MODEL)


def test_genuinely_empty_output_fails_and_names_the_reasoning_mode():
    with pytest.raises(AssertionError, match="inline as <think>"):
        assert_has_output(completion(text="", reasoning_text=""), INLINE_MODEL)
    with pytest.raises(AssertionError, match="reasoning_content"):
        assert_has_output(completion(text="", reasoning_text=""), FIELD_MODEL)


def test_usage_must_be_present_and_add_up():
    assert_usage_present_and_consistent(completion(usage=Usage(1, 2, 3)))
    with pytest.raises(AssertionError, match="prompt_tokens missing"):
        assert_usage_present_and_consistent(completion(usage=Usage(None, 2, 3)))
    with pytest.raises(AssertionError, match="does not add up"):
        assert_usage_present_and_consistent(completion(usage=Usage(1, 2, 99)))


THOUGHT = "17 times 20 is 340 and 17 times 3 is 51, so 340 plus 51 is 391"


@pytest.mark.parametrize(
    "answer",
    ["391", " 391.\n", "**391**", "The answer is 391.", "17 * 23 = 391", "391 (17 x 23)"],
)
@pytest.mark.parametrize("model", [INLINE_MODEL, FIELD_MODEL], ids=["inline", "field"])
def test_a_legitimate_answer_is_not_leaked_reasoning(answer, model):
    """Phrasing around the answer, and an answer the reasoning also reached, stay legitimate."""
    source = "field" if model is FIELD_MODEL else "inline"
    assert_answer_only(
        completion(text=answer, reasoning_text=THOUGHT, reasoning_source=source), model, "391"
    )


def test_an_exhausted_budget_is_left_to_the_answer_budget_cases():
    exhausted = completion(text="", reasoning_text=THOUGHT, finish_reason="length")
    assert_answer_only(exhausted, FIELD_MODEL, "391")


@pytest.mark.parametrize(
    "kw,model,cause",
    [
        (dict(text=f"{THOUGHT}. 391"), FIELD_MODEL, "characters, over the 35"),
        (dict(text=f"{THOUGHT}</think>391"), INLINE_MODEL, "markup ['</think>']"),
        (dict(text=f"<thinking>{THOUGHT}</thinking>391"), FIELD_MODEL, "markup"),
        (
            dict(
                text="391",
                reasoning_text=THOUGHT,
                reasoning_source="inline",
                reasoning_carrier="think",
            ),
            FIELD_MODEL,
            "inline as <think> in the answer channel, but the profile declares reasoning field",
        ),
        (dict(text="", reasoning_text=THOUGHT), FIELD_MODEL, "no answer text at a natural"),
        (dict(text="", reasoning_text=THOUGHT, finish_reason="end_turn"), FIELD_MODEL, "natural"),
        (dict(text="392"), FIELD_MODEL, "does not contain the expected answer '391'"),
    ],
)
def test_reasoning_in_the_answer_channel_fails(kw, model, cause):
    with pytest.raises(AssertionError, match="does not carry the answer alone") as error:
        assert_answer_only(completion(**kw), model, "391")
    assert cause in str(error.value)


def test_inline_reasoning_fails_where_no_reasoning_mode_is_declared():
    undeclared = Model(public_id="m")
    leaked = get("chat_completions").parse_completion(
        {"choices": [{"message": {"role": "assistant", "content": f"<think>{THOUGHT}</think>391"}}]}
    )
    assert_answer_only(leaked, INLINE_MODEL, "391")
    with pytest.raises(AssertionError, match="declares no inline reasoning"):
        assert_answer_only(leaked, undeclared, "391")


def test_messages_and_responses_mark_reasoning_that_arrived_inline():
    glued = f"<think>{THOUGHT}</think>391"
    messages = get("messages").parse_completion(
        {"type": "message", "content": [{"type": "text", "text": glued}]}
    )
    responses = get("responses").parse_completion(
        {"output": [{"type": "message", "content": [{"type": "output_text", "text": glued}]}]}
    )
    native = get("messages").parse_completion(
        {"content": [{"type": "thinking", "thinking": THOUGHT}, {"type": "text", "text": "391"}]}
    )
    for parsed in (messages, responses):
        assert (parsed.text, parsed.reasoning_source, parsed.reasoning_carrier) == (
            "391",
            "inline",
            "think",
        )
        with pytest.raises(AssertionError, match="inline as <think>"):
            assert_answer_only(parsed, FIELD_MODEL, "391")
    assert native.reasoning_source is None
    assert_answer_only(native, FIELD_MODEL, "391")
