"""The shapes a profile value can take, each checked strictly and reported with its path.

Every helper takes the value as YAML produced it and the path it was found at, and either
returns it unchanged or raises `ProfileError` naming that path. None of them converts between
types: a string where a list is expected is refused, not split into characters, and `"false"`
is not `false`. A profile that means something other than what it says must fail to load
rather than run a different test.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any

from .errors import ProfileError


def mapping(value: Any, where: str) -> dict[Any, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{where}: expected a mapping, got {value!r}")
    return value


def items(value: Any, where: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise ProfileError(f"{where}: expected a list, got {value!r}")
    return list(value)


def fields(
    value: Any, names: Iterable[str], where: str, nullable: Collection[str] | None = None
) -> dict[Any, Any]:
    """A mapping whose keys are all in `names`. With `nullable`, only those keys may be null."""
    raw = mapping(value, where)
    allowed = set(names)
    for name, item in raw.items():
        if name not in allowed:
            raise ProfileError(f"{where}.{name}: unknown field")
        if nullable is not None and item is None and name not in nullable:
            raise ProfileError(f"{where}.{name}: cannot be null")
    return raw


def flag(value: Any, default: bool, where: str) -> bool:
    """A declared boolean, or `default` when undeclared.

    `bool()` coerces, and that is the wrong behaviour for a declaration: every non-empty string is
    truthy, so `rate_limited: "false"` and `count_tokens: "off"` both read as True. YAML only
    produces a string here when the value was quoted or misspelled, and in both cases the author
    meant something they did not get.

    It matters most where a declaration RELAXES something -- `rate_limited: true` turns a 429 from
    a failure into a skip -- because a typo would silently switch off a check rather than
    switching one on.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ProfileError(f"{where}: expected true or false, got {value!r}")


def one_of(value: Any, allowed: tuple[str, ...], where: str, default: Any = None) -> Any:
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ProfileError(f"{where}: expected one of {list(allowed)}, got {value!r}")
    return value


def string(value: Any, where: str, *, non_empty: bool = False, allow_none: bool = False) -> Any:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or (non_empty and not value):
        kind = "a non-empty string" if non_empty else "a string"
        raise ProfileError(f"{where}: expected {kind}, got {value!r}")
    return value


def strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ProfileError(f"{where}: expected a list of strings, got {value!r}")
    return tuple(string(item, f"{where}[{index}]") for index, item in enumerate(value))
