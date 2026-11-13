"""Redaction has to hold at the logging layer, not only in the evidence writer.

httpx logs request headers at DEBUG and the run's log file is copied to --remote-output, so a
redactor that only covered serialised evidence would publish credentials. The logging-filter
test below is the one that actually guards that.
"""

from __future__ import annotations

import logging

import pytest

from llmdv.http import redact
from llmdv.log.filters import RedactingFilter


@pytest.fixture(autouse=True)
def _clean_registry():
    redact.reset_secrets()
    yield
    redact.reset_secrets()


def test_secret_headers_are_redacted_by_name():
    out = redact.redact_headers({"Authorization": "Bearer abc123", "X-Api-Key": "sk-x"})
    assert out["Authorization"] == redact.SENTINEL
    assert out["X-Api-Key"] == redact.SENTINEL


def test_registered_secret_is_scrubbed_from_an_unlisted_header():
    """A token can arrive in a header nobody thought to list."""
    redact.install_secrets(["sk-test-abcdef"])
    out = redact.redact_headers({"X-Custom-Thing": "sk-test-abcdef"})
    assert out["X-Custom-Thing"] == redact.SENTINEL


def test_short_values_are_not_registered():
    """A two-character secret would match everywhere and sentinel the whole log."""
    redact.install_secrets(["ab"])
    assert redact.redact_text("abcdef") == "abcdef"


def test_bearer_token_scrubbed_even_when_unregistered():
    assert "zzz" not in redact.scrub("sent Bearer zzzz9999")


def test_longest_secret_wins_so_no_readable_tail_remains():
    redact.install_secrets(["sk-abc", "sk-abcdef123"])
    assert redact.redact_text("key=sk-abcdef123") == f"key={redact.SENTINEL}"


def test_logging_filter_scrubs_interpolated_args():
    """The token arrives via record.args, so inspecting record.msg alone would miss it."""
    redact.install_secrets(["sk-secret-value"])
    record = logging.LogRecord(
        name="llmdv.test",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="headers=%s",
        args=({"Authorization": "Bearer sk-secret-value"},),
        exc_info=None,
    )
    assert RedactingFilter().filter(record) is True
    assert "sk-secret-value" not in record.getMessage()
    assert redact.SENTINEL in record.getMessage()
