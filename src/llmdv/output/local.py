"""The local run directory: the authoritative copy of a run's results and evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..log.setup import get_logger

# Not "output.*": that would nest under the primary-output logger, printing to stdout.
log = get_logger("sink.local")


def default_run_dir(root: Path | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return (root or Path("runs")) / stamp


class LocalDirSink:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def prepare(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        log.debug("run directory: %s", self.path)
        return self.path

    def write(self, relpath: str, data: str | bytes) -> Path:
        target = self.path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8")
        return target

    def publish(self, run_dir: Path) -> str | None:
        return str(self.path)

    def describe(self) -> str:
        return f"local:{self.path}"
