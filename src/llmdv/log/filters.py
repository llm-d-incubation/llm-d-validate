"""A logging filter that scrubs credentials out of every record.

Installed on every handler, not just the file handler. The threat is concrete: httpx logs
request headers at DEBUG, and the run's log file is copied to `--remote-output`.

Mutating `record.msg` and clearing `record.args` is deliberate. Formatting the message here
and handing the result on as a literal is the only way to scrub text that only exists after
`%`-interpolation -- a filter that inspected `record.msg` alone would miss a token that
arrived through `record.args`.
"""

from __future__ import annotations

import logging

from ..http import redact


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 -- a bad format string must not lose the record
            pass
        else:
            scrubbed = redact.scrub(message)
            if scrubbed != message:
                record.msg = scrubbed
                record.args = ()
        # Format once, then discard the raw exception reference from the record. Otherwise
        # a later formatter can bypass the scrubbed cache by formatting exc_info itself.
        # Do not mutate exception objects: they may still be used by the caller.
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact.scrub(record.exc_text)
        record.exc_info = None
        if record.stack_info:
            record.stack_info = redact.scrub(record.stack_info)
        return True
