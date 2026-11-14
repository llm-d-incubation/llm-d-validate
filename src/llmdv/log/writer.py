"""A file-like object that forwards writes into a logger.

`llmdv verify` embeds pytest, which writes its report to stdout. Rather than disabling
pytest's terminal plugin -- `-p no:terminal` breaks third-party plugins that look up
`terminalreporter` -- the pytest call is wrapped in redirect_stdout/redirect_stderr onto
one of these, so everything it emits arrives as log records under `llmdv.pytest` and obeys
`--log-level` and the redaction filter like anything else.

Reporting `isatty() -> False` is load-bearing, not incidental: it makes pytest and any
Rich-based plugin drop colour and cursor-control sequences by themselves, so no separate
"turn off colour" configuration is needed.
"""

from __future__ import annotations

import logging
import sys


class LoggerWriter:
    """Line-buffered adapter from a stream API onto a logger."""

    def __init__(self, logger_name: str, level: int) -> None:
        self._log = logging.getLogger(logger_name)
        self._level = level
        self._buf = ""

    # -- stream protocol ----------------------------------------------------

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)
        return len(text)

    def flush(self) -> None:
        if self._buf:
            self._emit(self._buf)
            self._buf = ""

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        """Return the real stderr descriptor, not an error.

        pytest's faulthandler plugin calls `sys.stderr.fileno()` during configure and does
        NOT catch OSError, so raising here aborts the whole session with an INTERNALERROR.

        Handing back the pristine descriptor is also the right answer on its merits: this is
        used for crash dumps, and a segfault traceback cannot travel through Python logging
        because the interpreter is already dying. It has to go to a real fd or be lost.
        """
        stream = sys.__stderr__
        if stream is None:  # embedded interpreters and pythonw have no console
            raise OSError("no real stderr available for LoggerWriter.fileno()")
        return stream.fileno()

    # -- internals ----------------------------------------------------------

    def _emit(self, line: str) -> None:
        # Blank lines are pytest's spacing, not information; dropping them keeps the log
        # readable without losing anything.
        if line.strip():
            self._log.log(self._level, "%s", line.rstrip())
