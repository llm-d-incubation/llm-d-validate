"""Parsing for the declarations the evolution cases read.

Each declaration says what a fleet does, so a change to the fleet or its clients fails
the case that names it. Parsing reports the path at fault, as the rest of the loader does.
"""

from __future__ import annotations

from typing import Any

from ..surfaces.base import KNOWN_SURFACES, SURFACE_PATHS
from .schema import (
    BYTE_FIDELITY_PROBES,
    CONTRACT_PARTS,
    CONTRACT_TYPES,
    FORWARD_COMPAT_OUTCOMES,
    FORWARD_COMPAT_PROBES,
    PROMPT_PINS,
    RENDER_PIN_DIGEST_LENGTH,
)

RENDER_SURFACES = frozenset(
    name for name, path in SURFACE_PATHS.items() if path.endswith("/render")
)
HEX = frozenset("0123456789abcdef")


def _error(message: str) -> Exception:
    from .loader import ProfileError

    return ProfileError(message)


# The key an outcome declared for every surface is stored under.
EVERY_SURFACE = "*"


def forward_compat(raw: Any, where: str) -> dict[str, dict[str, str]]:
    """Probe name to its declared outcome per surface.

    An outcome given as a string holds on every surface and is stored under `EVERY_SURFACE`; a
    mapping of surface to outcome declares only the surfaces it names.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _error(f"{where}: expected a mapping of probe to outcome, got {raw!r}")
    out: dict[str, dict[str, str]] = {}
    for probe, declared in raw.items():
        if probe not in FORWARD_COMPAT_PROBES:
            raise _error(
                f"{where}.{probe}: unknown probe; known: {', '.join(FORWARD_COMPAT_PROBES)}"
            )
        if isinstance(declared, dict) and declared:
            for surface in declared:
                if surface not in KNOWN_SURFACES:
                    raise _error(f"{where}.{probe}.{surface}: unknown surface")
            per = {str(surface): outcome for surface, outcome in declared.items()}
        else:
            per = {EVERY_SURFACE: declared}
        for at, outcome in per.items():
            if outcome not in FORWARD_COMPAT_OUTCOMES:
                place = f"{where}.{probe}" + ("" if at == EVERY_SURFACE else f".{at}")
                raise _error(
                    f"{place}: expected one of {', '.join(FORWARD_COMPAT_OUTCOMES)} or a "
                    f"mapping of surface to one of them, got {outcome!r}"
                )
        out[str(probe)] = {at: str(outcome) for at, outcome in per.items()}
    return out


def byte_fidelity(raw: Any, where: str) -> tuple[str, ...]:
    """Distinct probe names from the closed vocabulary."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) != len(set(map(str, raw))):
        raise _error(f"{where}: expected a list of distinct probe names, got {raw!r}")
    for name in raw:
        if name not in BYTE_FIDELITY_PROBES:
            raise _error(
                f"{where}: unknown probe {name!r}; known: {', '.join(BYTE_FIDELITY_PROBES)}"
            )
    return tuple(raw)


def prompt_pins(raw: Any, where: str) -> dict[str, dict[str, int]]:
    """Surface name to pin name to a positive input token count."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _error(f"{where}: expected a mapping of surface to pins, got {raw!r}")
    out: dict[str, dict[str, int]] = {}
    for surface, pins in raw.items():
        if surface not in KNOWN_SURFACES:
            raise _error(f"{where}.{surface}: unknown surface")
        if not isinstance(pins, dict):
            raise _error(f"{where}.{surface}: expected a mapping of pin to tokens, got {pins!r}")
        for name, tokens in pins.items():
            if name not in PROMPT_PINS:
                raise _error(
                    f"{where}.{surface}.{name}: unknown pin; known: {', '.join(PROMPT_PINS)}"
                )
            if type(tokens) is not int or tokens <= 0:
                raise _error(
                    f"{where}.{surface}.{name}: expected a positive integer, got {tokens!r}"
                )
        out[str(surface)] = {str(name): tokens for name, tokens in pins.items()}
    return out


def contract_baseline(raw: Any, where: str) -> dict[str, dict[str, dict[str, tuple[str, ...]]]]:
    """Surface name to part to key path to its allowed JSON types."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _error(f"{where}: expected a mapping of surface to parts, got {raw!r}")
    out: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {}
    for surface, parts in raw.items():
        if surface not in KNOWN_SURFACES:
            raise _error(f"{where}.{surface}: unknown surface")
        if not isinstance(parts, dict):
            raise _error(f"{where}.{surface}: expected a mapping of part to paths")
        out[str(surface)] = {}
        for part, paths in parts.items():
            at = f"{where}.{surface}.{part}"
            if part not in CONTRACT_PARTS:
                raise _error(f"{at}: unknown part; known: {', '.join(CONTRACT_PARTS)}")
            if not isinstance(paths, dict) or not paths:
                raise _error(f"{at}: expected a non-empty mapping of key path to types")
            parsed: dict[str, tuple[str, ...]] = {}
            for path, types in paths.items():
                types = [types] if isinstance(types, str) else types
                if (
                    not isinstance(path, str)
                    or not isinstance(types, list)
                    or not types
                    or any(item not in CONTRACT_TYPES for item in types)
                ):
                    raise _error(
                        f"{at}.{path}: expected a list of types from {', '.join(CONTRACT_TYPES)}"
                    )
                parsed[path] = tuple(types)
            out[str(surface)][str(part)] = parsed
    return out


def render_pins(raw: Any, where: str) -> dict[str, dict[str, str]]:
    """Render surface name to pin name to a lowercase hex SHA-256 digest of the token ids."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _error(f"{where}: expected a mapping of render surface to pins, got {raw!r}")
    out: dict[str, dict[str, str]] = {}
    for surface, pins in raw.items():
        if surface not in RENDER_SURFACES:
            known = ", ".join(sorted(RENDER_SURFACES))
            raise _error(f"{where}.{surface}: not a render surface; known: {known}")
        if not isinstance(pins, dict):
            raise _error(f"{where}.{surface}: expected a mapping of pin to digest, got {pins!r}")
        for name, digest in pins.items():
            if name not in PROMPT_PINS:
                raise _error(
                    f"{where}.{surface}.{name}: unknown pin; known: {', '.join(PROMPT_PINS)}"
                )
            if (
                not isinstance(digest, str)
                or len(digest) != RENDER_PIN_DIGEST_LENGTH
                or not set(digest) <= HEX
            ):
                raise _error(
                    f"{where}.{surface}.{name}: expected a lowercase hex SHA-256 digest, "
                    f"got {digest!r}"
                )
        out[str(surface)] = {str(name): digest for name, digest in pins.items()}
    return out
