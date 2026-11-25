"""MANIFEST.json -- published last, so a run is complete in storage if and only if it exists.

It means "publication complete", not "tests passed".
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .source import source_files, source_path

FILENAME = "MANIFEST.json"
SCHEMA = "llmdv.manifest/1"


def write(run_dir: Path) -> list[str]:
    """Hash every file of a finished run into MANIFEST.json; return their relative paths."""
    entries = []
    run_dir = run_dir.resolve(strict=True)
    for relpath in source_files(run_dir):
        if relpath == FILENAME:
            continue
        path = source_path(run_dir, relpath)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        entries.append({"path": relpath, "size": path.stat().st_size, "sha256": digest.hexdigest()})
    # Replace the directory entry rather than opening an existing MANIFEST.json: even a
    # link introduced after source validation must not redirect this write outside the run.
    temporary_path = run_dir / f".llmdv-manifest-{uuid4().hex}.tmp"
    temporary = None
    try:
        # Exclusive creation both refuses a substituted link and preserves normal umask
        # semantics (NamedTemporaryFile would make published file archives owner-only).
        with temporary_path.open("x", encoding="utf-8") as handle:
            temporary = temporary_path
            json.dump({"schema": SCHEMA, "files": entries}, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, run_dir / FILENAME)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return [entry["path"] for entry in entries]
