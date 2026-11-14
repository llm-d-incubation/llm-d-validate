"""profiles/REFERENCE.md documents exactly the keys the profile schema has.

A reference that drifts from the schema is worse than none: a reader trusts a key the loader no
longer reads, or never learns of one it does. So each heading in REFERENCE.md that holds a key
table is tied to the dataclass behind it, and the table's first column (a key in backticks) must
match that dataclass's fields in both directions.

Every dataclass reachable from `Profile` must be tied to a heading, so a new nested block fails
here until it is documented. The `fleet` block is the exception: its rules are documented in
FLEET.md and parsed in `profile/fleet.py`.
"""

from __future__ import annotations

import dataclasses
import re
import typing
from pathlib import Path

import pytest

from llmdv.profile import fleet, schema, transport

REFERENCE = Path(__file__).resolve().parents[1] / "profiles" / "REFERENCE.md"

# Heading text -> the dataclass whose fields its table documents.
SECTIONS = {
    "Top-level keys": schema.Profile,
    "environment": schema.Environment,
    "model": schema.Model,
    "model.reasoning": schema.Reasoning,
    "model.tools": schema.Tools,
    "ingresses": schema.Ingress,
    "ingresses[].auth": schema.Auth,
    "ingresses[].timeouts": schema.Timeouts,
    "ingresses[].tls": schema.Tls,
    "ingresses[].setup": schema.Setup,
    "ingresses[].setup.requires[]": schema.Requirement,
    "ingresses[].logs[]": schema.LogSource,
    "ingresses[].transport": transport.TransportContract,
    "ingresses[].transport.stream_budget": transport.StreamBudget,
    "capabilities": schema.Capabilities,
    "cache_reuse": schema.CacheReuse,
    "effort_rendering": schema.EffortRendering,
    "clients": schema.Client,
    "clients[].binaries[]": schema.ClientBinary,
    "known_failures": schema.KnownFailure,
}
# Fields whose YAML key differs from the attribute, and attributes the loader fills itself.
YAML_KEYS = {
    (schema.Reasoning, "field_name"): "field",
    (schema.KnownFailure, "ingresses"): "ingress",
}
NOT_IN_YAML = {(schema.Profile, "source_path")}


def yaml_keys(cls: type) -> set[str]:
    return {
        YAML_KEYS.get((cls, item.name), item.name)
        for item in dataclasses.fields(cls)
        if (cls, item.name) not in NOT_IN_YAML
    }


def documented_keys() -> dict[str, set[str]]:
    """Heading text -> the backticked keys in the first column of its table rows."""
    sections: dict[str, set[str]] = {}
    current = None
    for line in REFERENCE.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"#{2,4} (.+)", line)
        if heading:
            current = sections.setdefault(heading.group(1).strip(), set())
            continue
        row = re.match(r"\| `([^`]+)` \|", line)
        if row and current is not None:
            current.add(row.group(1))
    return sections


def nested_dataclasses(hint: object) -> set[type]:
    if dataclasses.is_dataclass(hint):
        return {hint}
    return {found for arg in typing.get_args(hint) for found in nested_dataclasses(arg)}


def reachable(cls: type, seen: set[type] | None = None) -> set[type]:
    """Dataclasses reachable from `cls` through field types, stopping at the fleet module."""
    seen = set() if seen is None else seen
    if cls in seen or cls.__module__ == fleet.__name__:
        return seen
    seen.add(cls)
    for hint in typing.get_type_hints(cls).values():
        for nested in nested_dataclasses(hint):
            reachable(nested, seen)
    return seen


@pytest.mark.parametrize("heading", sorted(SECTIONS))
def test_a_reference_table_matches_its_dataclass(heading: str):
    cls = SECTIONS[heading]
    documented = documented_keys().get(heading)
    assert documented is not None, f"REFERENCE.md has no heading {heading!r}"
    expected = yaml_keys(cls)
    assert not expected - documented, (
        f"{heading}: undocumented keys {sorted(expected - documented)}"
    )
    assert not documented - expected, (
        f"{heading}: documented keys {cls.__name__} lacks {sorted(documented - expected)}"
    )


def test_every_reachable_profile_block_has_a_reference_table():
    missing = reachable(schema.Profile) - set(SECTIONS.values())
    assert not missing, f"no REFERENCE.md heading for {sorted(c.__name__ for c in missing)}"
