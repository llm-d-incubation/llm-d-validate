"""Published logs retain diagnostic tracebacks without retaining their credentials."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sys

import pytest

from llmdv.http import redact
from llmdv.log import setup
from llmdv.log.filters import RedactingFilter


@pytest.fixture(autouse=True)
def clean_registry():
    redact.reset_secrets()
    yield
    setup.close_run_log()
    redact.reset_secrets()


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_chained_exceptions_are_redacted_in_the_published_log(tmp_path, fmt):
    secret = "registered-exception-credential"
    redact.install_secrets([secret])
    console = io.StringIO()
    path = tmp_path / "llmdv.log"
    with contextlib.redirect_stderr(console):
        setup.configure(level="DEBUG", fmt=fmt, run_log=path)
        try:
            try:
                raise ValueError(secret)
            except ValueError as cause:
                error = RuntimeError("request failed with Bearer unregistered-credential")
                error.__notes__ = [f"credential was {secret}"]
                raise error from cause
        except RuntimeError as error:
            setup.get_logger("test").exception("could not finish")
            # Logging must not rewrite the exception that the caller is handling.
            assert secret in error.__notes__[0]
    setup.close_run_log()
    text = path.read_text()
    for output in (text, console.getvalue()):
        assert secret not in output
        assert "unregistered-credential" not in output
    assert "ValueError" in text and "RuntimeError" in text
    assert redact.SENTINEL in text
    if fmt == "json":
        payload = json.loads(text)
        assert "Traceback" in payload["exc"]
        assert payload["msg"] == "could not finish"


@pytest.mark.parametrize("cached", [False, True])
def test_exception_and_stack_caches_are_safe_for_later_handlers(cached):
    secret = "cached-traceback-credential"
    redact.install_secrets([secret])
    try:
        raise RuntimeError(secret)
    except RuntimeError:
        record = logging.LogRecord(
            "llmdv.test", logging.INFO, __file__, 1, "failed", (), sys.exc_info()
        )
    if cached:
        # An earlier formatter may already have populated the standard logging cache.
        logging.Formatter().format(record)
    record.stack_info = f"Stack (most recent call last):\n{secret}"
    redactor = RedactingFilter()
    assert redactor.filter(record)
    assert redactor.filter(record)  # One shared record passes through multiple handlers.
    assert record.exc_info is None
    for formatter in (logging.Formatter(), setup._JsonFormatter(), setup._ConsoleFormatter()):
        text = formatter.format(record)
        assert secret not in text
        assert "RuntimeError" in text
        assert redact.SENTINEL in text
    assert secret not in record.stack_info


def test_bad_message_interpolation_does_not_bypass_exception_redaction():
    record = logging.LogRecord("llmdv.test", logging.ERROR, __file__, 1, "%d", ("bad",), None)
    record.exc_text = "RuntimeError: Bearer unknown-credential"
    record.stack_info = "Bearer stack-credential"
    assert RedactingFilter().filter(record)
    assert "unknown-credential" not in record.exc_text
    assert "stack-credential" not in record.stack_info
