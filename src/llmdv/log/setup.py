"""The logger tree. There are no `print` calls in llmdv; ruff rule T20 enforces it.

A CLI emits two different kinds of thing, and routing both through one stream breaks one of
them:

    diagnostics      progress, warnings, retries, traces      -> stderr, level-controlled
    primary output   the resolved profile, the run summary     -> stdout, always enabled

If primary output went through the normal logger at INFO, then `--log-level WARNING` would
silence the answer and `llmdv profile show | yq` would break. So `llmdv.output` is a separate
logger with `propagate = False` and its own stdout handler, and `--log-level` governs
diagnostics only -- which is what anyone setting it actually means.

Third-party libraries (httpx, openai, anthropic, urllib3) all use `logging`, so configuring
the tree captures them by propagation. httpx at DEBUG logs every request: valuable at -vv,
intolerable by default, so it is held one notch quieter than the console level.

Both console handlers keep the stream that was current when `configure()` ran. That binding
is load-bearing: `llmdv verify` then redirects stdout and stderr into `LoggerWriter`s (see
`writer.py`), and a handler that followed the redirect would feed its own output back into
the logger. When the bound stream has since been closed -- `configure()` ran under pytest's
capture, or under `redirect_stdout`, and that stream is gone -- the handler rebinds to the
current `sys.stdout` or `sys.stderr` rather than failing on every record, or to the
interpreter's original stream while the current one is a `LoggerWriter`. A CLI process never
closes its streams before exit, so there the two behave the same.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from .filters import RedactingFilter
from .writer import LoggerWriter

ROOT = "llmdv"
OUTPUT = "llmdv.output"

# Held one level quieter than the console unless the console is already at DEBUG.
NOISY = ("httpx", "httpcore", "openai", "anthropic", "urllib3")

_output_log = logging.getLogger(OUTPUT)

# The run directory's llmdv.log handler, kept so it can be closed before the bundle is hashed.
_run_log_handler: logging.Handler | None = None


class _TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"
        )


class _ConsoleFormatter(logging.Formatter):
    """Bare message on the console, with a level prefix only when the level matters."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            return f"{record.levelname.lower()}: {message}"
        if record.exc_info:
            return f"{message}\n{self.formatException(record.exc_info)}"
        return message


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, for pod and CI consumption."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class _ConsoleHandler(logging.StreamHandler):
    """A StreamHandler on `sys.stdout` or `sys.stderr` that outlives a stream closed under it."""

    def __init__(self, attribute: str) -> None:
        # Not `_name`: logging.Handler already uses that for the handler's own name.
        self._sys_attribute = attribute
        super().__init__(getattr(sys, attribute))

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self.stream, "closed", False):
            current = getattr(sys, self._sys_attribute)
            # Never rebind onto a stream that feeds back into logging: that recurses. Under a
            # redirect, fall back to the interpreter's original stream instead; staying on the
            # closed one recurses too, through handleError() writing into the same redirect.
            if current is None or isinstance(current, LoggerWriter):
                current = getattr(sys, f"__{self._sys_attribute}__")
            # Assigned, not setStream(): that flushes the old stream, which raises once closed.
            # emit() already runs under the handler's lock.
            if current is not None and not getattr(current, "closed", False):
                self.stream = current
        super().emit(record)


def _formatter(fmt: str) -> logging.Formatter:
    return _JsonFormatter() if fmt == "json" else _TextFormatter()


def configure(
    level: str = "INFO",
    fmt: str = "text",
    log_file: Path | None = None,
    run_log: Path | None = None,
) -> None:
    """Build the logger tree. Idempotent: existing handlers are replaced, not stacked.

    `run_log` always receives full DEBUG regardless of the console level. That is the whole
    reproducibility argument -- the console stays readable while the evidence bundle keeps
    everything.
    """
    global _run_log_handler
    numeric = getattr(logging, level.upper(), logging.INFO)
    redactor = RedactingFilter()
    _run_log_handler = None

    root = logging.getLogger(ROOT)
    root.setLevel(logging.DEBUG)  # handlers do the filtering, so a DEBUG file sink works
    root.handlers.clear()
    root.propagate = False

    console = _ConsoleHandler("stderr")
    console.setLevel(numeric)
    # Bare on the console, timestamped in the files. Twenty-two repetitions of
    # "22:00:53 INFO llmdv.plugin:" wrapped around the content is noise on a terminal and
    # essential in a log being read after the fact, so the two sinks are formatted differently.
    # A warning or error keeps its level prefix, because there the level IS the message.
    console.setFormatter(_ConsoleFormatter() if fmt == "text" else _formatter(fmt))
    console.addFilter(redactor)
    root.addHandler(console)

    for path, sink_level in ((run_log, logging.DEBUG), (log_file, numeric)):
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setLevel(sink_level)
        handler.setFormatter(_formatter(fmt))
        handler.addFilter(redactor)
        root.addHandler(handler)
        if path is run_log:
            _run_log_handler = handler

    # Primary output: stdout, bare message, never filtered by --log-level.
    out = logging.getLogger(OUTPUT)
    out.setLevel(logging.INFO)
    out.handlers.clear()
    out.propagate = False
    stdout = _ConsoleHandler("stdout")
    stdout.setLevel(logging.INFO)
    stdout.setFormatter(logging.Formatter("%(message)s"))
    stdout.addFilter(redactor)
    out.addHandler(stdout)

    # Third-party loggers propagate into our root; give them their own ceiling.
    noisy_level = logging.DEBUG if numeric <= logging.DEBUG else max(numeric, logging.WARNING)
    for name in NOISY:
        third = logging.getLogger(name)
        third.setLevel(noisy_level)
        third.handlers.clear()
        third.propagate = True

    logging.captureWarnings(True)
    logging.getLogger("py.warnings").propagate = True


def close_run_log() -> None:
    """Stop writing the run directory's llmdv.log; later diagnostics still reach the console."""
    global _run_log_handler
    if _run_log_handler is not None:
        logging.getLogger(ROOT).removeHandler(_run_log_handler)
        _run_log_handler.close()
        _run_log_handler = None


def emit(message: str = "") -> None:
    """Write one line of primary output to stdout.

    Deliberately a named function rather than a bare logger call: it makes every primary
    output site greppable, and it is what replaces `print` throughout the codebase.
    """
    _output_log.info("%s", message)


def get_logger(name: str) -> logging.Logger:
    """Logger for a llmdv module. `name` is appended under the `llmdv` root."""
    return logging.getLogger(name if name.startswith(ROOT) else f"{ROOT}.{name}")
