"""Validate the source of a publication before reading files from a frozen run."""

from __future__ import annotations

from pathlib import Path

from .local import RUN_STATE


class UnsafeSource(ValueError):
    """A run contains a source path that cannot safely be published."""


def source_path(run_dir: Path, relative: str) -> Path:
    """Resolve a source inside the run; never follow it outside the run directory.

    Resolve again at each read, not just when building the manifest: a file replaced with
    an escaping link during an upload must not expose the target. As with the manifest's
    hashes, publication assumes the run is otherwise frozen while it is being read.
    """
    root = run_dir.resolve(strict=True)
    try:
        target = (root / relative).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UnsafeSource(f"unreadable publication source: {relative!r}") from exc
    if not target.is_relative_to(root) or target == root:
        raise UnsafeSource(f"publication source escapes the run directory: {relative!r}")
    if target.is_relative_to(root / RUN_STATE) != (Path(relative).parts[0] == RUN_STATE):
        raise UnsafeSource(f"publication source links across private run state: {relative!r}")
    return target


def source_files(run_dir: Path) -> list[str]:
    """Validate the entire tree before hashing or uploading any file.

    Directory links are checked too, even though pathlib does not recurse into them.
    Contained file links retain their published names and contents. Broken links fail
    explicitly instead of silently disappearing from the manifest.
    """
    root = run_dir.resolve(strict=True)
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        target = source_path(root, relative)
        if target.is_file():
            if path.relative_to(root).parts[0] != RUN_STATE:
                files.append(relative)
        elif not target.is_dir():
            raise UnsafeSource(f"publication source is not a regular file: {relative!r}")
    return files
