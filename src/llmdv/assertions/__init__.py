"""Assertion helpers, shared across surfaces so each lesson is encoded once."""

from typing import NoReturn

from ..evidence import journal
from ..http.redact import scrub
from .common import (
    assert_answer_only,
    assert_has_answer,
    assert_has_output,
    assert_healthy_finish,
    assert_not_aborted,
    assert_usage_present_and_consistent,
)
from .errors import (
    assert_client_error,
    assert_context_limit_error,
    assert_error_envelope,
    assert_no_generation,
    assert_not_throttled,
    preview,
    tolerates,
)

__all__ = [
    "fail",
    "assert_not_throttled",
    "assert_client_error",
    "assert_context_limit_error",
    "assert_error_envelope",
    "assert_answer_only",
    "assert_has_answer",
    "assert_has_output",
    "assert_healthy_finish",
    "assert_no_generation",
    "preview",
    "assert_not_aborted",
    "tolerates",
    "assert_usage_present_and_consistent",
]


def fail(name: str, observed: object, expected: object) -> NoReturn:
    """Record the deciding evidence before raising an assertion failure."""
    observed, expected = scrub(str(observed)), scrub(str(expected))
    journal.record(name, observed, expected, ok=False)
    raise AssertionError(f"{name}: expected {expected}, got {observed!r}")
