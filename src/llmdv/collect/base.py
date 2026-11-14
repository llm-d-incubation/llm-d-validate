"""Collecting a layer's own account of what happened.

An HTTP response says a request failed; the engine's log says why. Attribution across three
layers is guesswork without both, which is why the design treats this as part of the evidence
rather than as a debugging convenience.

The protocol exists so the kubectl implementation is not the only possible one -- an
in-cluster run with a log aggregator, or a fixture-driven test, can satisfy the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..profile.schema import Profile


@dataclass
class CollectedLog:
    """One container's logs for the window of a run."""

    ingress_id: str
    pod: str
    container: str
    path: str | None = None  # relative to the run directory
    lines: int = 0
    truncated: bool = False
    error: str | None = None
    artifact: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ingress": self.ingress_id,
            "pod": self.pod,
            "container": self.container,
            "path": self.path,
            "lines": self.lines,
            "truncated": self.truncated,
            "error": self.error,
            "artifact": self.artifact,
        }


@dataclass
class Collection:
    logs: list[CollectedLog] = field(default_factory=list)
    # Why there are no logs, when there are none. Without it "nothing failed so we did not
    # collect" and "we were denied" are the same empty list, and a published run cannot be
    # read honestly by anyone who was not there when it ran.
    reason: str | None = None

    @property
    def ok(self) -> list[CollectedLog]:
        return [item for item in self.logs if item.path and not item.error]

    @property
    def failed(self) -> list[CollectedLog]:
        return [item for item in self.logs if item.error]


class LogCollector(Protocol):
    def collect(
        self,
        profile: Profile,
        ingress_ids: list[str],
        since_time: str | None,
        out_dir: Path,
    ) -> Collection: ...

    def describe(self) -> str: ...
