"""Redaction, in one place, used by every consumer.

There are three places a credential can escape -- a log record, an evidence file, and a
generated curl -- and a redactor that only covers one of them is a false sense of safety.
httpx logs request headers at DEBUG, so `--log-level DEBUG` without the logging filter
writes the bearer token into `llmdv.log`, which `--remote-output` would then copy to a PVC.

So this module is the single source of truth and the other three call into it. Secrets are
registered once, when a profile resolves its `key_env` variables, and the registry is
process-global because the logging filter has no other way to reach them.
"""

from __future__ import annotations

import re
from typing import Any

SENTINEL = "***REDACTED***"

# Header names whose value is always a credential, regardless of profile.
SECRET_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "api-key",
        "cookie",
        "set-cookie",
        "proxy-authorization",
    }
)

# Literal secret values registered from a profile's resolved key_env variables.
_SECRETS: set[str] = set()


def install_secrets(values: list[str] | set[str]) -> None:
    """Register literal secret values for scrubbing out of free text.

    Short values are ignored: a one- or two-character "secret" would match constantly and
    turn every log line into sentinels.
    """
    for value in values:
        if value and len(value) >= 4:
            _SECRETS.add(value)


def reset_secrets() -> None:
    """Drop the registry. For tests, and for a CLI run that re-resolves a profile."""
    _SECRETS.clear()


def known_secrets() -> frozenset[str]:
    return frozenset(_SECRETS)


def redact_text(text: str) -> str:
    """Replace every registered secret literal in `text`.

    Longest first, so a secret that contains another secret as a prefix does not leave a
    readable tail behind.
    """
    if not text:
        return text
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, SENTINEL)
    return text


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Redact by header name, then scrub registered literals from what remains.

    The second pass matters: a token can arrive in a header nobody thought to list, and a
    name-only redactor would pass it straight through.
    """
    if not headers:
        return {}
    out: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in SECRET_HEADERS:
            out[name] = SENTINEL
        else:
            out[name] = redact_text(str(value))
    return out


_BEARER = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)


def redact_bearer(text: str) -> str:
    """Scrub `Bearer <token>` even when the token was never registered.

    Catches a credential that reached a log line from somewhere other than the profile --
    an SDK default, an environment variable we do not know about.
    """
    return _BEARER.sub(rf"\1{SENTINEL}", text)


def scrub(text: str) -> str:
    """Full text scrub: registered literals plus any bearer token."""
    return redact_bearer(redact_text(text))


def redact_value(value: Any) -> Any:
    """Scrub every string inside a JSON-shaped value: dicts, lists, and the strings in them.

    For anything written as JSON into the run directory. Scrubbing the serialised text instead
    would let the bearer pattern swallow a closing quote and corrupt the file, and a body is
    only redacted at all if something walks into it: a gateway that echoes the key back in a
    JSON error body would otherwise land in exchanges.jsonl, response.json and the results.
    """
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value
