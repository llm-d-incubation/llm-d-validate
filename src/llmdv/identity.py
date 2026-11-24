"""The fixed configuration identity of a verification run."""

from __future__ import annotations

from typing import Any

from . import __version__
from .profile.schema import Profile

DIMENSIONS = (
    "guide",
    "accelerator",
    "engine",
    "provider",
    "gateway",
    "model",
    "llm_d_ref",
    "llmdv_version",
    "profile_digest",
    "trigger",
    "flavor",
)
CONFIGURATION_DIMENSIONS = (
    "guide",
    "accelerator",
    "engine",
    "provider",
    "gateway",
    "model",
    "trigger",
    "flavor",
)
# `flavor` names whatever else tells lanes that share a profile apart (two cache offload
# backends, say), so their runs never become one another's baselines.
LANE_DIMENSIONS = ("accelerator", "provider", "llm_d_ref", "trigger", "flavor")
ASSERTION_DIMENSIONS = ("guide", "engine", "gateway", "model")
_ALIASES = {"llm-d-ref": "llm_d_ref", "llm-d_ref": "llm_d_ref"}
RESERVED_LABELS = frozenset(DIMENSIONS) | {
    "llm-d-ref",
    "llm-d_ref",
    "llmdv-version",
    "profile-digest",
}


class IdentityError(ValueError):
    """A lane identity is malformed or contradicts the profile."""


def _layer_implementation(profile: Profile, layer: str) -> str | None:
    """The product a profile names for a layer, at the top level (`engine`, `gateway`)."""
    return getattr(profile, layer, None)


def parse(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        key = _ALIASES.get(key, key)
        if not sep or key not in (*LANE_DIMENSIONS, *ASSERTION_DIMENSIONS) or not value.strip():
            raise IdentityError(
                "--identity must be one of key=value for "
                f"{', '.join((*LANE_DIMENSIONS, *ASSERTION_DIMENSIONS))}; "
                f"got {item!r}"
            )
        # A stray space from shell quoting or YAML would make a lane of its own, which matches
        # no earlier run: its regressions would never be compared.
        if value != value.strip():
            raise IdentityError(f"--identity {key}: {value!r} has leading or trailing whitespace")
        # A later value replaces an earlier one, so a caller can override the runner's default
        # (`llmdv job` passes its own `--identity` before the caller's).
        result[key] = value
    result.setdefault("trigger", "manual")
    return result


def build(
    profile: Profile, lane: dict[str, str], profile_digest: str | None
) -> dict[str, str | None]:
    """Build one identity; only lane dimensions may be supplied by the caller."""
    values: dict[str, str | None] = {
        "guide": profile.guide,
        "accelerator": lane.get("accelerator"),
        "engine": _layer_implementation(profile, "engine"),
        "provider": lane.get("provider"),
        "gateway": _layer_implementation(profile, "gateway"),
        "model": profile.model.public_id,
        "llm_d_ref": lane.get("llm_d_ref"),
        "llmdv_version": __version__,
        "profile_digest": profile_digest,
        "trigger": lane.get("trigger", "manual"),
        "flavor": lane.get("flavor"),
    }
    return values


def validate_lane(identity: dict[str, str], actual: dict[str, str | None]) -> None:
    mismatches = [
        f"{key}={identity[key]!r} (profile has {actual.get(key)!r})"
        for key in identity
        if identity[key] != actual.get(key)
    ]
    if mismatches:
        raise IdentityError("identity mismatch: " + ", ".join(mismatches))


def differences(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[str]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    return [
        f"{key}: {before.get(key)!r} -> {after.get(key)!r}"
        for key in DIMENSIONS
        if before.get(key) != after.get(key)
    ]
