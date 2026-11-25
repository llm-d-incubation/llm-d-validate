"""Names shared by archive readers, publishers and artifact stores."""

from __future__ import annotations

from pathlib import Path


class UnsafeName(ValueError):
    """A name that would address something outside its archive location."""


def safe(value: str, what: str) -> str:
    """Validate one profile or run-id component without changing its identity."""
    text = str(value)
    if not text or text.startswith(".") or any(c in text for c in ("/", "\\", "\x00")):
        raise UnsafeName(f"unsafe {what}: {value!r}")
    return text


def run_name(name: str) -> str:
    """A published run always occupies exactly <profile>/<run-id>."""
    parts = name.split("/")
    if len(parts) != 2:
        raise UnsafeName(f"unsafe run location: {name!r}; expected <profile>/<run-id>")
    return f"{safe(parts[0], 'profile name')}/{safe(parts[1], 'run id')}"


def relative_key(*parts: str) -> str:
    """Join relative object paths, rejecting traversal instead of normalizing it away."""
    for part in parts:
        if (
            not part
            or "\\" in part
            or "\x00" in part
            or any(component in {"", ".", ".."} for component in part.split("/"))
        ):
            raise UnsafeName(f"unsafe object path: {part!r}")
    if not parts:
        raise UnsafeName("an object path is required")
    return "/".join(parts)


def contained_path(root: Path, *parts: str) -> Path:
    """Resolve existing symlinks and require the destination to remain below the root."""
    root = root.expanduser().resolve()
    target = (root / relative_key(*parts)).resolve()
    if not target.is_relative_to(root) or target == root:
        raise UnsafeName(f"object path escapes the archive: {'/'.join(parts)!r}")
    return target
