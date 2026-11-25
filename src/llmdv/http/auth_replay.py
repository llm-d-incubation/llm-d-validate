"""Credential provenance for replay, without retaining credential material."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping

AUTH_HEADERS = frozenset({"authorization", "x-api-key", "api-key"})
SYNTHETIC_KEY = "llmdv-invalid"


def capture(headers: Mapping[str, str], configured: Mapping[str, str] | None = None) -> list[dict]:
    """Record shape and whether the token matches a configured credential.

    The descriptors contain only fixed vocabulary. An unrecognised token is never stored or
    replaced with a valid credential during replay, even when it came through an SDK hook.
    """
    # Match this ingress's configured credential, not the process-wide redaction registry:
    # a credential belonging to another ingress must not become this ingress's key.
    secrets = set()
    for name, value in (configured or {}).items():
        if name.lower() not in AUTH_HEADERS:
            continue
        if name.lower() == "authorization" and value.lower().startswith("bearer "):
            value = value[7:]
        if value:
            secrets.add(value)
    records = []
    for name, value in headers.items():
        name = name.lower()
        if name not in AUTH_HEADERS:
            continue
        if not value:
            kind = "empty"
        elif name != "authorization":
            kind = "profile_key" if value in secrets else "replacement_key"
        elif value.lower() == "bearer":
            kind = "bearer_without_token"
        elif value.lower().startswith("bearer "):
            kind = "profile_bearer" if value[7:] in secrets else "replacement_bearer"
        elif value.lower().startswith("basic "):
            try:
                base64.b64decode(value[6:], validate=True)
            except (binascii.Error, ValueError):
                kind = "malformed_basic"
            else:
                kind = "replacement_basic"
        elif " " not in value:
            kind = "profile_key" if value in secrets else "replacement_key"
        else:
            kind = "unavailable"
        records.append({"header": name, "kind": kind})
    return records


def header_arguments(
    headers: Mapping[str, str], provenance: list[dict] | None, key_env: str | None
) -> tuple[list[str], bool, list[str]]:
    """Return curl header values, whether they need the key, and unreplayable headers."""
    kinds = {item.get("header"): item.get("kind") for item in (provenance or [])}
    arguments, unavailable = [], []
    needs_key = False
    replacements = {
        "bearer_without_token": "Bearer",
        "replacement_bearer": f"Bearer {SYNTHETIC_KEY}",
        "replacement_key": SYNTHETIC_KEY,
        "replacement_basic": "Basic "
        + base64.b64encode(f"llmdv:{SYNTHETIC_KEY}".encode()).decode(),
        "malformed_basic": "Basic llmdv-invalid",
    }
    for name, value in headers.items():
        if name.lower() not in AUTH_HEADERS:
            arguments.append(f"{name}: {value}")
            continue
        kind = kinds.get(name.lower())
        if kind == "empty":
            # `Header:` removes a header in curl; `Header;` transmits an empty value.
            arguments.append(f"{name};")
            continue
        if kind in {"profile_bearer", "profile_key"} and key_env:
            value = ("Bearer " if kind == "profile_bearer" else "") + f"${{{key_env}}}"
            needs_key = True
        elif kind in replacements:
            value = replacements[kind]
        else:
            # Older bundles discarded credential shape. Guessing from the profile can turn
            # their negative-auth requests into successful authenticated requests.
            unavailable.append(name)
            continue
        arguments.append(f"{name}: {value}")
    return arguments, needs_key, unavailable


def unavailable_script() -> str:
    return (
        "# Authentication cannot be replayed: missing credential provenance or profile key.\n"
        "# Inspect the original credential case before constructing the request.\n"
        "exit 2"
    )
