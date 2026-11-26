"""The local run directory: the authoritative copy of a run's results and evidence."""

from __future__ import annotations

import re
from pathlib import Path

from ..log.setup import get_logger
from ..results.record import new_run_id

# Not "output.*": that would nest under the primary-output logger, printing to stdout.
log = get_logger("sink.local")

# Local reservation and crash-recovery state. Excluded from publication.
RUN_STATE = ".llmdv-started"


def default_run_dir(root: Path | None = None, profile_name: str = "run") -> Path:
    # A display label, never a path supplied by the profile. The recorded profile name and
    # run identity remain unchanged, including the timestamp-first ids used by compare.
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", profile_name).strip(".-")[:100] or "run"
    return (root or Path("runs")) / f"{label}-{new_run_id()}"


def create_run_dir(root: Path, profile_name: str) -> Path:
    """Reserve a fresh directory under the output root, even for simultaneous runs."""
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    while True:
        path = default_run_dir(root, profile_name)
        try:
            path.mkdir()
        except FileExistsError:
            continue
        log.debug("run directory: %s", path)
        return path


def claim_run_dir(path: Path) -> Path:
    """Claim an empty job reservation exactly once, including simultaneous starts.

    Keep the empty marker directory even if startup fails before writing an outcome: that
    reservation has been consumed. An empty directory contributes no published artifacts.
    """
    path = path.resolve()
    if not path.is_dir() or any(path.iterdir()):
        raise FileExistsError(f"the job's reserved run directory must exist and be empty: {path}")
    # The emptiness check is diagnostic; exclusive mkdir is the actual claim. Two children
    # may both observe an empty reservation, but only one can create this marker.
    (path / RUN_STATE).mkdir(mode=0o700)
    return path


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
