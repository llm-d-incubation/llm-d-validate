"""Primary output and diagnostics must not share a stream.

If primary output went through the normal logger, `--log-level WARNING` would silence the
answer and `llmdv profile show | yq` would break. These assert the split holds.
"""

from __future__ import annotations

import contextlib
import io

from llmdv.log import setup
from llmdv.log.writer import LoggerWriter


def _capture(level="INFO"):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        setup.configure(level=level)
        setup.emit("PRIMARY")
        setup.get_logger("probe").info("DIAGNOSTIC")
        setup.get_logger("probe").debug("DEBUGLINE")
    return out.getvalue(), err.getvalue()


def test_primary_output_goes_to_stdout_only():
    out, err = _capture()
    assert "PRIMARY" in out
    assert "PRIMARY" not in err


def test_diagnostics_go_to_stderr_only():
    out, err = _capture()
    assert "DIAGNOSTIC" in err
    assert "DIAGNOSTIC" not in out


def test_primary_output_survives_a_quiet_log_level():
    """The whole point: raising the level must not suppress the answer."""
    out, err = _capture(level="ERROR")
    assert "PRIMARY" in out
    assert "DIAGNOSTIC" not in err


def test_debug_is_suppressed_at_info():
    _, err = _capture(level="INFO")
    assert "DEBUGLINE" not in err


def test_configure_is_idempotent_and_does_not_stack_handlers():
    setup.configure(level="INFO")
    first = len(setup.logging.getLogger(setup.ROOT).handlers)
    setup.configure(level="INFO")
    assert len(setup.logging.getLogger(setup.ROOT).handlers) == first


def _configure_under_a_stream_that_then_closes() -> None:
    """What an in-process `llmdv` run inside pytest does: configure under capture, which closes."""
    stale_out, stale_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stale_out), contextlib.redirect_stderr(stale_err):
        setup.configure(level="INFO")
    stale_out.close()
    stale_err.close()


def test_diagnostics_follow_stderr_after_the_configured_stream_closes():
    _configure_under_a_stream_that_then_closes()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        setup.get_logger("probe").info("LATER-DIAGNOSTIC")
    assert "LATER-DIAGNOSTIC" in err.getvalue()
    assert "Logging error" not in err.getvalue()


def test_primary_output_follows_stdout_after_the_configured_stream_closes():
    _configure_under_a_stream_that_then_closes()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        setup.emit("LATER-PRIMARY")
    assert "LATER-PRIMARY" in out.getvalue()
    assert "Logging error" not in err.getvalue()


def test_diagnostics_stay_on_the_bound_stream_while_verify_redirects_into_the_logger():
    """`llmdv verify` redirects stdout and stderr into LoggerWriters after configuring; the
    console must keep writing to the real stream, or each record would loop back into logging."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        setup.configure(level="DEBUG")
        with contextlib.redirect_stderr(LoggerWriter("llmdv.pytest", setup.logging.WARNING)):
            setup.get_logger("probe").info("DURING-REDIRECT")
    assert "DURING-REDIRECT" in err.getvalue()


def test_a_closed_stream_under_verify_redirect_falls_back_to_the_original_stream(monkeypatch):
    """Bound stream closed and stdout/stderr redirected into LoggerWriters: neither is safe, so
    the console falls back to the interpreter's own streams rather than recursing."""
    _configure_under_a_stream_that_then_closes()
    real_out, real_err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(setup.sys, "__stdout__", real_out)
    monkeypatch.setattr(setup.sys, "__stderr__", real_err)
    with (
        contextlib.redirect_stdout(LoggerWriter("llmdv.pytest", setup.logging.DEBUG)),
        contextlib.redirect_stderr(LoggerWriter("llmdv.pytest", setup.logging.WARNING)),
    ):
        setup.get_logger("probe").info("DURING-REDIRECT")
        setup.emit("PRIMARY-DURING-REDIRECT")
    assert "DURING-REDIRECT" in real_err.getvalue()
    assert "PRIMARY-DURING-REDIRECT" in real_out.getvalue()
