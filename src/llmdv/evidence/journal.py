"""A per-case record of what was actually checked, and what was observed.

A verdict without evidence is an assertion about an assertion. "PASS" tells you a function did
not raise; it does not tell you the engine returned `finish_reason: "stop"` with 14+64=78
tokens, which is the thing anyone auditing the run actually wants to see -- and it is the only
way to tell a real pass from a test that silently checked nothing.

So the assertion helpers record what they looked at, both when they are satisfied and when they
are not. Every check lands in `llmdv.log` (always full DEBUG, whatever the console level), in
`results.jsonl` under `checks`, and in the evidence bundle. A failing check is recorded BEFORE
the assertion fires, so the journal shows the observed value that caused the failure rather
than stopping just short of it.

The active journal is process-global because assertion helpers are plain functions called deep
inside a test; threading a context object through every one of them would be the only
alternative, and it would make the helpers harder to use than the bare `assert` they replace.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..log.setup import get_logger

log = get_logger("evidence.checks")


@dataclass
class Check:
    """One observation an assertion made."""

    name: str
    observed: Any
    expected: str | None = None
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        mark = "ok" if self.ok else "FAILED"
        against = f" (expected {self.expected})" if self.expected else ""
        return f"{mark}: {self.name}={self.observed!r}{against}"


@dataclass
class Journal:
    nodeid: str
    checks: list[Check] = field(default_factory=list)

    def record(
        self, name: str, observed: Any, expected: str | None = None, ok: bool = True
    ) -> Check:
        check = Check(name=name, observed=observed, expected=expected, ok=ok)
        self.checks.append(check)
        log.debug("%s | %s", self.nodeid.split("::")[-1], check.render())
        return check

    def digest(self, limit: int = 3) -> str:
        """A short one-line summary for the progress line.

        Capped: the point is a glanceable confirmation that something real was checked, not a
        full transcript. The full set is in llmdv.log and results.jsonl.
        """
        if not self.checks:
            return ""
        parts = [f"{c.name}={c.observed}" for c in self.checks[:limit]]
        if len(self.checks) > limit:
            parts.append(f"+{len(self.checks) - limit} more")
        return " · ".join(parts)


_ACTIVE: Journal | None = None


def activate(nodeid: str) -> Journal:
    global _ACTIVE
    _ACTIVE = Journal(nodeid=nodeid)
    return _ACTIVE


def current() -> Journal | None:
    return _ACTIVE


def deactivate() -> Journal | None:
    global _ACTIVE
    finished, _ACTIVE = _ACTIVE, None
    return finished


def record(name: str, observed: Any, expected: str | None = None, ok: bool = True) -> None:
    """Record one observation against the active case, if there is one.

    A no-op outside a test, so the assertion helpers remain usable in isolation -- including
    from llmdv's own unit tests, which call them without a journal.
    """
    if _ACTIVE is not None:
        _ACTIVE.record(name, observed, expected, ok)
