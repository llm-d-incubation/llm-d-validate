"""Offline authoring views over the same profile and source layers the loader uses.

Exports preserve declarations, including environment-variable names. Explanations show parsed
values and their assignments, with profile-wide model/capability defaults applied before ingress
overrides. Neither view reveals values read from the environment.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from enum import Enum
from pathlib import Path
from typing import Any

from . import compose
from .errors import ProfileError
from .schema import KnownFailure, Profile, Reasoning


@dataclasses.dataclass(frozen=True)
class Assignment:
    source: Path
    path: str
    value: Any


@dataclasses.dataclass(frozen=True)
class ExplainedValue:
    path: str
    value: Any
    assignments: tuple[Assignment, ...]


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            (
                "field"
                if isinstance(value, Reasoning) and f.name == "field_name"
                else "ingress"
                if isinstance(value, KnownFailure) and f.name == "ingresses"
                else f.name
            ): _plain(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.name != "source_path"
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


def _path(parts: tuple[str, ...]) -> str:
    text = ""
    for index, part in enumerate(parts):
        if (index == 1 and parts[0] == "ingresses") or any(c in part for c in ".[]"):
            text += f"[{part}]"
        else:
            text += ("." if text else "") + part
    return text


def _leaves(value: Any, parts: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _leaves(child, (*parts, str(key)))
    elif parts == ("ingresses",):
        for ingress in value:
            yield from _leaves(ingress, (*parts, ingress["id"]))
    else:
        yield parts, value


def _assignment(layer: compose.Layer, parts: tuple[str, ...]) -> Assignment | None:
    """The declaration affecting this field, including replacement of a whole ancestor.

    Lists replace as a whole, except the root ingress list, which is keyed by id. Empty
    mappings merge and therefore do not clear an inherited child.
    """
    node: Any = layer.raw
    for index, part in enumerate(parts):
        if index == 1 and parts[0] == "ingresses" and isinstance(node, list):
            node = next((item for item in node if item.get("id") == part), None)
            if node is None:
                return None
        elif isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
        else:
            return Assignment(layer.path, _path(parts[:index]), node)
    return Assignment(layer.path, _path(parts), node)


def explain(profile: Profile, chain: list[compose.Layer]) -> list[ExplainedValue]:
    """Effective values with source assignments in precedence order, defaults included.

    Lists are one value, matching their replacement semantics. Ingresses are the exception:
    each has its own fields and inherits model/capabilities independently of file order.
    """
    view = _plain(profile)
    for ingress in view["ingresses"]:
        if ingress["base_url_env"]:
            ingress["base_url"] = f"${{{ingress['base_url_env']}}}"

    rows = []
    for parts, value in _leaves(view):
        assignments = []
        if len(parts) > 2 and parts[0] == "ingresses" and parts[2] in {"model", "capabilities"}:
            assignments.extend(
                found for layer in chain if (found := _assignment(layer, parts[2:])) is not None
            )
        assignments.extend(
            found for layer in chain if (found := _assignment(layer, parts)) is not None
        )
        if parts == ("name",):
            assignments = [
                Assignment(
                    chain[-1].path,
                    "name" if chain[-1].raw.get("name") else "(file name)",
                    profile.name,
                )
            ]
        if len(parts) == 3 and parts[0] == "ingresses" and parts[2] == "base_url":
            if isinstance(value, str) and value.startswith("${"):
                assignments = [
                    found
                    for layer in chain
                    if (found := _assignment(layer, (*parts[:2], "base_url_env"))) is not None
                ]
        rows.append(ExplainedValue(_path(parts), value, tuple(assignments)))
    return rows


def explain_text(profile: Profile, chain: list[compose.Layer], field: str | None = None) -> str:
    rows = explain(profile, chain)
    if field:
        rows = [
            row
            for row in rows
            if row.path == field or row.path.startswith((field + ".", field + "["))
        ]
        if not rows:
            raise ProfileError(f"no profile field {field!r}; use profile explain without --field")
    else:
        # The default view stays short. Query an omitted field explicitly to see its default.
        rows = [row for row in rows if row.assignments]
    lines = [
        "Effective values. Source assignments follow in precedence order.",
        "Model/capability defaults apply before ingress overrides. Environment values are hidden.",
        "Use --field to inspect a field or block, including omitted fields and library defaults.",
        "",
    ]
    for row in rows:
        lines.append(f"{row.path} = {json.dumps(row.value, ensure_ascii=False, default=str)}")
        if not row.assignments:
            lines.append("  from llmdv default")
        for item in row.assignments:
            lines.append(
                f"  from {item.source}: {item.path} = "
                f"{json.dumps(item.value, ensure_ascii=False, default=str)}"
            )
    return "\n".join(lines)


def export_yaml(chain: list[compose.Layer]) -> str:
    """A standalone declaration, preserving variable references and clearing extends."""
    return compose.render(chain, standalone=True)
