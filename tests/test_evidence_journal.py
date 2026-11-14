"""The check journal: what makes a PASS auditable rather than a bare claim.

A verdict with no recorded observations is indistinguishable from a test that checked nothing,
so these assert that the helpers record on success as well as on failure, and that a failing
check is recorded with the observed value rather than lost when the assertion fires.
"""

from __future__ import annotations

import pytest

from llmdv.assertions import (
    assert_has_output,
    assert_healthy_finish,
    assert_usage_present_and_consistent,
)
from llmdv.evidence import journal
from llmdv.profile.schema import Model, Reasoning
from llmdv.surfaces.base import Completion, Usage

INLINE_MODEL = Model(public_id="m", reasoning=Reasoning(inline_tag="think", default_on=True))


@pytest.fixture(autouse=True)
def _journal():
    journal.activate("test::case[fake]")
    yield
    journal.deactivate()


def completion(**kw) -> Completion:
    base = dict(
        role="assistant",
        text="ok",
        finish_reason="stop",
        usage=Usage(14, 64, 78),
        reasoning_text="",
    )
    base.update(kw)
    return Completion(**base)


def test_a_passing_assertion_records_what_it_saw():
    assert_healthy_finish(completion())
    assert_usage_present_and_consistent(completion())
    recorded = {c.name: c.observed for c in journal.current().checks}
    assert recorded["finish_reason"] == "stop"
    assert recorded["usage"] == "14+64=78"
    assert all(c.ok for c in journal.current().checks)


def test_a_failing_assertion_records_the_observed_value_before_raising():
    """The journal must show the value that caused the failure, not stop just short of it."""
    with pytest.raises(AssertionError):
        assert_healthy_finish(completion(finish_reason="abort"))
    failed = [c for c in journal.current().checks if not c.ok]
    assert failed and failed[0].name == "finish_reason"
    assert failed[0].observed == "abort"


def test_reasoning_only_output_is_recorded_with_both_counts():
    """Proof that content was empty for a legitimate reason, not that nothing came back."""
    assert_has_output(completion(text="", reasoning_text="weighing it up"), INLINE_MODEL)
    recorded = {c.name: c.observed for c in journal.current().checks}
    assert recorded["output_chars"] == "content=0 reasoning=14 tools=0"


def test_digest_is_capped_and_flags_the_remainder():
    for index in range(6):
        journal.record(f"check{index}", index)
    digest = journal.current().digest(limit=3)
    assert digest.startswith("check0=0 · check1=1 · check2=2")
    assert "+3 more" in digest


def test_recording_outside_a_case_is_a_no_op():
    """The helpers stay usable in isolation, including from llmdv's own unit tests."""
    journal.deactivate()
    journal.record("orphan", 1)  # must not raise
    assert journal.current() is None
    assert_healthy_finish(completion())  # must not raise either


def test_each_case_gets_a_fresh_journal():
    journal.record("first", 1)
    journal.activate("test::other[fake]")
    assert journal.current().checks == []
