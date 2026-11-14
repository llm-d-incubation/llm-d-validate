"""LoggerWriter stands in for stdout while pytest runs inside `llmdv verify`."""

from __future__ import annotations

import logging

from llmdv.log.writer import LoggerWriter


def test_lines_are_emitted_only_on_newline(caplog):
    writer = LoggerWriter("llmdv.pytest", logging.INFO)
    with caplog.at_level(logging.INFO, logger="llmdv.pytest"):
        writer.write("partial")
        assert not caplog.records  # still buffered
        writer.write(" line\n")
        assert [r.getMessage() for r in caplog.records] == ["partial line"]


def test_flush_emits_a_trailing_partial_line(caplog):
    writer = LoggerWriter("llmdv.pytest", logging.INFO)
    with caplog.at_level(logging.INFO, logger="llmdv.pytest"):
        writer.write("no trailing newline")
        writer.flush()
        assert [r.getMessage() for r in caplog.records] == ["no trailing newline"]


def test_blank_lines_are_dropped(caplog):
    """pytest emits blank lines for spacing; they carry nothing and would pad the log."""
    writer = LoggerWriter("llmdv.pytest", logging.INFO)
    with caplog.at_level(logging.INFO, logger="llmdv.pytest"):
        writer.write("\n\n   \n")
        writer.flush()
    assert caplog.records == []


def test_isatty_is_false_so_colour_is_disabled_downstream():
    assert LoggerWriter("llmdv.pytest", logging.INFO).isatty() is False


def test_fileno_returns_a_real_descriptor():
    """pytest's faulthandler calls this during configure and does not catch OSError.

    Returning a real fd is what stops the whole session aborting with an INTERNALERROR.
    """
    assert isinstance(LoggerWriter("llmdv.pytest", logging.INFO).fileno(), int)
