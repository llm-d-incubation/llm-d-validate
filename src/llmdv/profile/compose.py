"""`extends`: one profile written as changes to another.

A profile may name one parent with a top-level `extends: <path>`, relative to its own file. The
parent may extend another, so a profile is a chain of files resolved root first. Each file is
merged over the one before it by the rule a per-ingress override already uses: mappings merge
key by key, a list or any other value replaces, and `null` clears. `ingresses` is keyed by `id`,
the name the rest of a profile and every error path use, so it merges like a mapping: a child
entry merges over the parent's entry with the same id by the same rule, and an entry with a new
id is added at the end. A child can then add `setup` to one ingress without restating the others.

The composed mapping is what the loader validates, so a profile is checked exactly as it runs,
and a problem is reported against the file that set the value at fault.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import reduce
from pathlib import Path
from typing import Any

import yaml

from .errors import ProfileError


@dataclass(frozen=True)
class Layer:
    """One file of a chain, as read, without its `extends` key."""

    path: Path
    raw: dict[str, Any]


def merge(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge mappings without mutating either input; scalars, lists and null replace."""
    merged = dict(defaults)
    for name, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(name), dict):
            merged[name] = merge(merged[name], value)
        else:
            merged[name] = value
    return merged


def compose(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """`child` over `parent` by `merge`, with ingresses keyed by id as a mapping is."""
    merged = merge(parent, child)
    ours, theirs = parent.get("ingresses"), child.get("ingresses")
    if isinstance(ours, list) and isinstance(theirs, list):
        merged["ingresses"] = _ingresses(ours, theirs)
    return merged


def _ingresses(parent: list[Any], child: list[Any]) -> list[Any]:
    out = list(parent)
    for entry in child:
        at = _position(out, entry.get("id")) if isinstance(entry, dict) else None
        if at is None:
            out.append(entry)
        else:
            out[at] = merge(out[at], entry)
    return out


def _position(entries: list[Any], ingress_id: Any) -> int | None:
    if ingress_id is None:
        return None
    for index, entry in enumerate(entries):
        if isinstance(entry, dict) and entry.get("id") == ingress_id:
            return index
    return None


def read(path: Path) -> dict[str, Any]:
    """One profile file as a mapping, with every error naming the file."""
    if not path.is_file():
        raise ProfileError(f"no such profile file: {path}")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"{path}: top level must be a mapping")
    return raw


def layers(path: str | Path) -> list[Layer]:
    """The chain of files `path` extends, root first, ending with `path` itself."""
    chain: list[Layer] = []
    current = Path(path)
    while True:
        raw = read(current)
        chain.insert(
            0, Layer(current, {key: value for key, value in raw.items() if key != "extends"})
        )
        parent = raw.get("extends")
        if parent is None:
            return chain
        if not isinstance(parent, str) or not parent:
            raise ProfileError(
                f"{current}: extends: expected the path of a parent profile, got {parent!r}"
            )
        resolved = Path(os.path.normpath(current.parent / parent))
        if not resolved.is_file():
            raise ProfileError(f"{current}: extends: no such profile file: {resolved}")
        if any(resolved.resolve() == layer.path.resolve() for layer in chain):
            names = " -> ".join(str(layer.path) for layer in reversed(chain))
            raise ProfileError(f"{current}: extends: a cycle: {names} -> {resolved}")
        current = resolved


def composed(chain: list[Layer]) -> dict[str, Any]:
    return reduce(compose, (layer.raw for layer in chain[1:]), chain[0].raw)


# A diagnostic path such as `ingresses[0](engine).model.reasoning`, one part at a time.
_PART = re.compile(r"\.?([^.\[(]+)|\[(\d+)\]|\(([^)]*)\)")


def origin(chain: list[Layer], where: str) -> Path:
    """The file that set the value at a diagnostic path.

    The last file in the chain that sets the path, or replaces or clears a block on the way to
    it. A problem about something no file declares, such as a missing required key, belongs to
    the file that was loaded.
    """
    if len(chain) > 1 and where and not any(c.isspace() for c in where):
        parts = _PART.findall(where)
        for layer in reversed(chain):
            if _sets(layer.raw, parts):
                return layer.path
    return chain[-1].path


def _sets(node: Any, parts: list[tuple[str, str, str]]) -> bool:
    for key, index, entry_id in parts:
        if entry_id:
            at = _position(node, entry_id) if isinstance(node, list) else None
            if at is None:
                return False  # an ingress this file does not name
            node = node[at]
        elif index:
            continue  # lists replace whole; ingresses are keyed by the id that follows
        elif isinstance(node, dict):
            if key not in node:
                return False
            node = node[key]
        else:
            return True  # this file replaced or cleared a block on the way
    return True


def render(chain: list[Layer]) -> str:
    """The composed profile as YAML, headed by which file set each key.

    It loads exactly as the chain does, so a run directory needs no other file to repeat the
    run. Comments in the source files are not carried over.
    """
    profile = composed(chain)
    if "name" not in profile:
        profile = {"name": chain[-1].path.stem, **profile}
    leaf = chain[-1].path.parent

    def named(layer: Layer) -> str:
        return os.path.relpath(layer.path, leaf)

    def setters(found) -> str:
        return ", ".join(named(layer) for layer in chain if found(layer.raw)) or "(file name)"

    lines = [
        "# Composed by llmdv from these profiles, each merged over the one before it:",
        *(f"#   {named(layer)}" for layer in chain),
        "# Where each key was set:",
    ]
    for key in profile:
        lines.append(f"#   {key}: {setters(lambda raw, key=key: key in raw)}")
        if key == "ingresses" and isinstance(profile[key], list):
            for index, entry in enumerate(profile[key]):
                if not isinstance(entry, dict):
                    continue
                entry_id = entry.get("id")
                found = setters(
                    lambda raw, entry_id=entry_id: (
                        isinstance(raw.get("ingresses"), list)
                        and _position(raw["ingresses"], entry_id) is not None
                    )
                )
                lines.append(f"#     ingresses[{index}]({entry_id}): {found}")
    body = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=100)
    return "\n".join(lines) + "\n" + body
