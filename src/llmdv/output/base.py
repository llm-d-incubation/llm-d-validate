"""Where a run's artifacts go.

Local is always the source of truth, even when a remote destination is requested: remote is a
publish step over a finished local bundle, never a stream target. In a pod that keeps the
suite working when upload credentials are wrong, and it means the reproducibility guarantee
never depends on a network hop completing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class OutputSink(Protocol):
    def prepare(self) -> Path: ...

    def write(self, relpath: str, data: str | bytes) -> Path: ...

    def publish(self, run_dir: Path) -> str | None: ...

    def describe(self) -> str: ...


class ObjectSink(Protocol):
    def describe(self) -> str: ...

    def check(self) -> None: ...

    def put_bytes(self, data: bytes, *parts: str, content_type: str | None = None) -> str: ...
