"""Library diagnostics reach the run evidence through the same redaction boundary."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import warnings

import pytest

from llmdv.http import redact
from llmdv.log import setup
from llmdv.log.writer import LoggerWriter


@pytest.fixture(autouse=True)
def restore_logging():
    names = ("", setup.ROOT, setup.OUTPUT, *setup.NOISY, "py.warnings")
    saved = {
        name: (log.handlers[:], log.level, log.propagate)
        for name in names
        for log in (logging.getLogger(name),)
    }
    showwarning = warnings.showwarning
    redact.reset_secrets()
    yield
    setup.close_run_log()
    for name, (handlers, level, propagate) in saved.items():
        log = logging.getLogger(name)
        for handler in log.handlers[:]:
            if handler not in handlers:
                handler.close()
        log.handlers[:] = handlers
        log.setLevel(level)
        log.propagate = propagate
    warnings.showwarning = showwarning
    redact.reset_secrets()


@pytest.mark.parametrize("fmt", ["text", "json"])
@pytest.mark.parametrize("name", [*setup.NOISY, "httpcore.connection", "openai._base_client"])
def test_debug_evidence_is_redacted_even_with_a_quiet_console(tmp_path, fmt, name):
    path = tmp_path / "run.log"
    secret = "registered-library-credential"
    redact.install_secrets([secret])
    out, err, unrelated = io.StringIO(), io.StringIO(), io.StringIO()
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler(unrelated))
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        setup.configure(level="INFO", fmt=fmt, run_log=path)
        logger = logging.getLogger(name)
        logger.debug("request credential: %s", secret)
        logger.info("ordinary request")
        logger.warning("library warning")
    setup.close_run_log()

    text = path.read_text()
    assert secret not in text
    assert text.count("request credential:") == 1
    assert text.count("ordinary request") == 1
    assert text.count("library warning") == 1
    assert redact.SENTINEL in text
    assert "request credential" not in err.getvalue()
    assert "ordinary request" not in err.getvalue()
    assert err.getvalue().count("library warning") == 1
    assert not out.getvalue()
    assert not unrelated.getvalue()
    if fmt == "json":
        records = [json.loads(line) for line in text.splitlines()]
        assert [record["logger"] for record in records] == [name] * 3


def test_reconfigure_closes_old_log_and_routes_only_to_new_handlers(tmp_path):
    first, second = tmp_path / "first.log", tmp_path / "second.log"
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        setup.configure(level="DEBUG", run_log=first)
        old = setup._run_log_handler
        logging.getLogger("httpx").debug("first run")
        setup.configure(level="DEBUG", run_log=second)
        assert old.stream is None
        logging.getLogger("httpx").debug("second run")
        setup.close_run_log()
        logging.getLogger("httpx").debug("after close")

    assert "second run" not in first.read_text()
    assert second.read_text().count("second run") == 1
    assert "after close" not in second.read_text()
    assert err.getvalue().count("second run") == 1
    assert err.getvalue().count("after close") == 1


def test_forwarding_does_not_recurse_during_pytest_stream_redirection():
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        setup.configure(level="DEBUG")
        with contextlib.redirect_stderr(LoggerWriter("llmdv.pytest", logging.WARNING)):
            logging.getLogger("anthropic").debug("during redirect")
    assert err.getvalue().count("during redirect") == 1


def test_warnings_are_captured_without_changing_unrelated_root_logging(tmp_path):
    path = tmp_path / "run.log"
    unrelated = io.StringIO()
    root = logging.getLogger()
    handler = logging.StreamHandler(unrelated)
    root.addHandler(handler)
    with contextlib.redirect_stderr(io.StringIO()):
        setup.configure(run_log=path)
        logging.getLogger("py.warnings").warning("captured diagnostic")
        logging.getLogger("unrelated.application").warning("application warning")
    setup.close_run_log()
    assert "captured diagnostic" in path.read_text()
    assert "application warning" not in path.read_text()
    assert unrelated.getvalue() == "application warning\n"
    assert handler in root.handlers
