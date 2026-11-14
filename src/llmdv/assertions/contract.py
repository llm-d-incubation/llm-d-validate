"""Contract drift: a response's key paths and value types against a declared baseline.

An engine or gateway release renames a field, moves usage to another frame, or changes a type,
and every client reading the old shape breaks while the response still parses. A shape is the
set of JSON types seen at each key path of a body: `choices[].message.content` for a field
inside an array element. Stream frames that carry a string `type` are prefixed with it, so
`message_start:message.usage.input_tokens` names a field of one event; a bare
`message_start:` path is the event itself.

A declared path must be present with one of its declared types, or declare `absent` among them.
A path whose parent is absent or is not an object or array is not required, so an optional
object's fields bind only when it is there. Paths the baseline does not name are new: recorded,
never failed, because adding a field breaks no client.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..evidence import journal
from . import fail

Shape = dict[str, set[str]]
# How many path names a failure message or a new-path record carries.
SHOWN = 8


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object" if isinstance(value, dict) else type(value).__name__


def _join(path: str, key: str) -> str:
    return f"{path}{key}" if not path or path.endswith(":") else f"{path}.{key}"


def _walk(value: Any, path: str, out: Shape) -> None:
    out.setdefault(path, set()).add(json_type(value))
    if isinstance(value, dict):
        for key, child in value.items():
            _walk(child, _join(path, str(key)), out)
    elif isinstance(value, list):
        for child in value:
            _walk(child, f"{path}[]", out)


def shape(body: Any) -> Shape:
    """Key paths and types of one JSON object."""
    out: Shape = {}
    if isinstance(body, dict):
        for key, child in body.items():
            _walk(child, str(key), out)
    return out


def stream_shape(frames: Iterable[Any]) -> Shape:
    """The union of every frame's shape, each prefixed by the frame's own `type` if it has one."""
    out: Shape = {}
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        kind = frame.get("type")
        root = f"{kind}:" if isinstance(kind, str) and kind else ""
        if root:
            out.setdefault(root, set()).add("object")
        for key, child in frame.items():
            _walk(child, _join(root, str(key)), out)
    return out


def baseline(observed: Shape) -> dict[str, list[str]]:
    """A declarable baseline from an observed shape, for a profile author to start from."""
    return {path: sorted(types) for path, types in sorted(observed.items())}


def _parent(path: str) -> str:
    prefix, colon, rest = path.rpartition(":")
    prefix = prefix + colon if colon else ""
    rest = rest if colon else path
    if not rest:
        return ""
    if rest.endswith("[]"):
        return prefix + rest[:-2]
    head, dot, _ = rest.rpartition(".")
    return prefix + head if dot else prefix


def _required(path: str, observed: Shape) -> bool:
    parent = _parent(path)
    if parent == "":
        return True
    if parent.endswith(":"):
        return parent in observed
    return bool(observed.get(parent, set()) & {"object", "array"})


def compare(observed: Shape, declared: Mapping[str, Iterable[str]]):
    """(missing, retyped, new): paths declared but absent, typed otherwise, and undeclared."""
    missing, retyped = [], []
    for path, types in declared.items():
        allowed = set(types)
        if path not in observed:
            if "absent" not in allowed and _required(path, observed):
                missing.append(path)
        elif not observed[path] <= allowed - {"absent"}:
            retyped.append(f"{path} ({'|'.join(sorted(observed[path]))})")
    new = sorted(path for path in observed if path not in declared)
    return sorted(missing), sorted(retyped), new


def assert_contract(observed: Shape, declared: Mapping[str, Iterable[str]], where: str) -> None:
    """Record the comparison, then fail on the first missing or retyped path by name."""
    missing, retyped, new = compare(observed, declared)
    journal.record("contract_paths_declared", len(declared))
    journal.record(f"{where}_new_paths", new[:SHOWN] + (["..."] if len(new) > SHOWN else []))
    journal.record(f"{where}_missing", missing[:SHOWN], "none", ok=not missing)
    journal.record(f"{where}_retyped", retyped[:SHOWN], "none", ok=not retyped)
    if missing:
        fail("contract_missing", ", ".join(missing[:SHOWN]), f"every declared {where} path")
    if retyped:
        fail("contract_retyped", ", ".join(retyped[:SHOWN]), f"the declared {where} types")
