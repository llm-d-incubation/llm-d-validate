"""Evaluate declared configuration rules against objects already read from the cluster.

Pure functions over the JSON `kubectl get -o json` returns, so every rule is testable without a
cluster. Reading is the case's job, through `KubeContext.get_objects`; nothing here talks to
anything.
"""

from __future__ import annotations

import re
from typing import Any

from ..profile.fleet import ConfigRule, Extraction

_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, None: 1.0}


def object_name(obj: dict) -> str:
    return str((obj.get("metadata") or {}).get("name") or "?")


def _container_units_in(obj: Any) -> list[tuple[tuple[str, ...], dict]]:
    found: list[tuple[tuple[str, ...], dict]] = []

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = (*path, key)
                if (
                    key == "containers"
                    and isinstance(value, list)
                    and all(isinstance(item, dict) and "name" in item for item in value)
                ):
                    found.extend(((*here, str(index)), item) for index, item in enumerate(value))
                else:
                    walk(value, here)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, (*path, str(index)))

    walk(obj, ())
    return found


def containers_in(obj: Any) -> list[dict]:
    """Every container of every pod template inside an object, in document order.

    Walks the whole object, so a Deployment, a StatefulSet, a Pod and a custom resource that
    nests several pod templates (one per role) are read the same way.
    """
    return [container for _, container in _container_units_in(obj)]


def configmap_refs(obj: Any) -> set[str]:
    """ConfigMaps the object's containers take environment variables from."""
    return set(configmap_requirements(obj))


def configmap_requirements(obj: Any) -> dict[str, bool]:
    """Referenced ConfigMaps and whether at least one reference requires each one."""
    names: dict[str, bool] = {}
    for container in containers_in(obj):
        for entry in container.get("env") or []:
            ref = ((entry or {}).get("valueFrom") or {}).get("configMapKeyRef") or {}
            if ref.get("name"):
                name = str(ref["name"])
                names[name] = names.get(name, False) or ref.get("optional") is not True
        for source in container.get("envFrom") or []:
            ref = (source or {}).get("configMapRef") or {}
            if ref.get("name"):
                name = str(ref["name"])
                names[name] = names.get(name, False) or ref.get("optional") is not True
    return names


def container_text(container: dict, configmaps: dict[str, dict] | None = None) -> str:
    """A container as text: its command line, then one NAME=value line per variable.

    Variables from a ConfigMap, one by one or whole through `envFrom`, render with their values
    when `configmaps` holds that ConfigMap's data, so a setting moved into a ConfigMap is still
    seen. Anything else set by reference, a Secret above all, renders as NAME=<from>: a rule
    can see that it is set without llmdv reading what it points at.
    """
    configmaps = configmaps or {}
    words = [str(word) for word in (container.get("command") or []) + (container.get("args") or [])]
    variables: dict[str, Any] = {}
    for source in container.get("envFrom") or []:
        ref = (source or {}).get("configMapRef") or {}
        data = configmaps.get(str(ref.get("name")))
        if data is None:
            continue
        prefix = str((source or {}).get("prefix") or "")
        for key in sorted(data):
            variables[f"{prefix}{key}"] = data[key]
    for entry in container.get("env") or []:
        if not isinstance(entry, dict) or "name" not in entry:
            continue
        value = entry.get("value")
        if value is None:
            ref = (entry.get("valueFrom") or {}).get("configMapKeyRef") or {}
            value = (configmaps.get(str(ref.get("name"))) or {}).get(str(ref.get("key")))
        variables[str(entry["name"])] = "<from>" if value is None else value
    lines = [" ".join(words), *(f"{name}={value}" for name, value in variables.items())]
    return "\n".join(lines)


def units(
    obj: dict,
    view: str,
    containers: tuple[str, ...] = (),
    configmaps: dict[str, dict] | None = None,
) -> list[tuple[str, str]]:
    """The labelled pieces of text a rule judges in one object."""
    name = object_name(obj)
    if view == "data":
        data = obj.get("data") or {}
        return [(name, "\n".join(str(data[key]) for key in sorted(data)))]
    out = []
    candidates = _container_units_in(obj)
    name_counts: dict[str, int] = {}
    for _, container in candidates:
        key = str(container.get("name"))
        name_counts[key] = name_counts.get(key, 0) + 1
    for path, container in candidates:
        if containers and container.get("name") not in containers:
            continue
        container_name = str(container.get("name"))
        suffix = f"@{'.'.join(path)}" if name_counts[container_name] > 1 else ""
        out.append((f"{name}/{container_name}{suffix}", container_text(container, configmaps)))
    return out


def settings_violations(pieces: list[tuple[str, str]], rule: ConfigRule) -> dict[str, list[str]]:
    """Per unit, the required patterns missing and the forbidden ones present."""
    out: dict[str, list[str]] = {}
    for label, text in pieces:
        problems = [
            f"missing {pattern!r}"
            for pattern in rule.require
            if not re.search(pattern, text, re.MULTILINE)
        ]
        for pattern in rule.forbid:
            found = re.search(pattern, text, re.MULTILINE)
            if found:
                problems.append(f"forbidden {pattern!r} matched {found.group(0)[:120]!r}")
        if problems:
            out[label] = problems
    return out


def extract(
    objects: list[dict], extraction: Extraction, configmaps: dict[str, dict] | None = None
) -> tuple[dict[str, str], list[str]]:
    """Values by unit label, and the labels that yielded none and have no default."""
    values: dict[str, str] = {}
    missing: list[str] = []
    for obj in objects:
        for label, text in units(obj, extraction.view, extraction.containers, configmaps):
            found = re.findall(extraction.pattern, text, re.MULTILINE)
            distinct = sorted(set(found))
            if len(distinct) == 1:
                values[label] = distinct[0]
            elif len(distinct) > 1:
                # Two values in one unit already disagree; keep both visible.
                for index, value in enumerate(distinct):
                    values[f"{label}#{index}"] = value
            elif extraction.default is not None:
                values[label] = extraction.default
            else:
                missing.append(label)
    return values, missing


def dig(obj: Any, path: tuple[str, ...]) -> Any:
    """The value at a key path, or None. Keys are literal, so annotation names need no escaping."""
    node = obj
    for key in path:
        if isinstance(node, dict):
            node = node.get(key)
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return None
    return node


def duration_seconds(value: Any) -> float | None:
    """A timeout in seconds from a number or a string such as `2700s`, `45m` or `1h`."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", stripped):
        return float(stripped)
    parts = list(_DURATION.finditer(stripped))
    if not parts or "".join(item.group(0).replace(" ", "") for item in parts) != stripped.replace(
        " ", ""
    ):
        return None
    return sum(float(item.group(1)) * _UNIT_SECONDS[item.group(2)] for item in parts)


def ready_addresses(endpoints: dict) -> list[str]:
    """Ready addresses of an Endpoints object, by pod name where known, else by IP."""
    out = []
    for subset in endpoints.get("subsets") or []:
        for address in subset.get("addresses") or []:
            ref = address.get("targetRef") or {}
            out.append(str(ref.get("name") or address.get("ip") or "?"))
    return out


def ready_in_slices(slices: list[dict]) -> list[str]:
    """Ready endpoints across a Service's EndpointSlices, once each.

    A missing `ready` condition means ready, as the EndpointSlice API defines it. A dual-stack
    Service lists the same pod in one slice per address family, so pods are counted by name.
    """
    found = set()
    for item in slices:
        for endpoint in item.get("endpoints") or []:
            if (endpoint.get("conditions") or {}).get("ready") is False:
                continue
            ref = endpoint.get("targetRef") or {}
            addresses = endpoint.get("addresses") or ["?"]
            found.add(str(ref.get("name") or addresses[0]))
    return sorted(found)


def selects(selector: Any, labels: dict) -> bool:
    """Whether an equality selector (a label map) selects a pod with these labels."""
    if not isinstance(selector, dict) or not selector:
        return False
    return all(labels.get(key) == value for key, value in selector.items())


def selector_text(selector: dict) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
