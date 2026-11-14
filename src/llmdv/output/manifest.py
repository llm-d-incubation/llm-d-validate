"""MANIFEST.json -- published last, so a run is complete in storage if and only if it exists.

It means "publication complete", not "tests passed".
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

FILENAME = "MANIFEST.json"
SCHEMA = "llmdv.manifest/1"


def write(run_dir: Path) -> list[str]:
    """Hash every file of a finished run into MANIFEST.json; return their relative paths."""
    entries = []
    for path in sorted(p for p in run_dir.rglob("*") if p.is_file()):
        relpath = path.relative_to(run_dir).as_posix()
        if relpath == FILENAME:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        entries.append({"path": relpath, "size": path.stat().st_size, "sha256": digest.hexdigest()})
    (run_dir / FILENAME).write_text(
        json.dumps({"schema": SCHEMA, "files": entries}, indent=2) + "\n", encoding="utf-8"
    )
    return [entry["path"] for entry in entries]
