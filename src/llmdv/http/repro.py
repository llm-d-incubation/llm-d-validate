"""Turn a recorded exchange into a runnable curl.

The point is a failure that can be handed to someone else, or revisited next week, without
the original terminal. So the script carries no credential: where auth was used it reads the
same `key_env` variable the profile names, which makes the file shareable as-is.
"""

from __future__ import annotations

import json
import re
import shlex

from ..profile.schema import Auth, AuthType
from . import redact
from .client import Exchange

SKIP_HEADERS = frozenset({"host", "content-length", "connection", "accept-encoding", "user-agent"})


def to_curl(
    exchange: Exchange, auth: Auth | None = None, setup_hint: list[str] | None = None
) -> str:
    """Render a self-contained shell script reproducing one exchange."""
    lines = [
        "#!/usr/bin/env bash",
        "# Reproduces one llmdv exchange. Generated; no credential is embedded.",
        "set -euo pipefail",
        "",
    ]

    if auth and auth.type is not AuthType.NONE and auth.key_env:
        lines += [
            "# This ingress authenticates. Export the same variable the profile names:",
            f': "${{{auth.key_env}:?set {auth.key_env} before running this script}}"',
            "",
        ]

    if setup_hint:
        lines.append("# This endpoint is reached from outside the cluster through a forward:")
        lines += [f"#   {line}" for line in setup_hint]
        lines.append("")

    args = ["curl", "-sS", "-i", "-X", exchange.method, exchange.url]

    for name, value in (exchange.request_headers or {}).items():
        if name.lower() in SKIP_HEADERS:
            continue
        if value == redact.SENTINEL and auth and auth.key_env:
            # Rebuild the real header from the environment rather than printing a sentinel
            # that would be sent literally.
            if name.lower() == "authorization":
                args += ["-H", f"Authorization: Bearer ${{{auth.key_env}}}"]
            else:
                args += ["-H", f"{name}: ${{{auth.key_env}}}"]
            continue
        args += ["-H", f"{name}: {value}"]

    if exchange.request_body is not None:
        body = (
            json.dumps(exchange.request_body, indent=2)
            if not isinstance(exchange.request_body, str)
            else exchange.request_body
        )
        args += ["--data-binary", "@-"]
        rendered = " ".join(_quote(part) for part in args)
        lines.append(f"{rendered} <<'BODY'")
        lines.append(body)
        lines.append("BODY")
    else:
        lines.append(" ".join(_quote(part) for part in args))

    lines.append("")
    return "\n".join(lines)


_VAR = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _quote(part: str) -> str:
    """Shell-quote a word, leaving an intentional ${VAR} reference expandable.

    A word holding ${VAR} is double-quoted with every other `$`, backtick, quote and backslash
    escaped, so the named variable is the only thing the shell expands. Single quotes, which
    this used to produce for a header such as `Authorization: Bearer $KEY`, send the variable
    name literally.
    """
    if not _VAR.search(part):
        return shlex.quote(part)
    out, last = [], 0
    for match in _VAR.finditer(part):
        out.append(_escape_double(part[last : match.start()]))
        out.append(match.group(0))
        last = match.end()
    out.append(_escape_double(part[last:]))
    return '"' + "".join(out) + '"'


def _escape_double(text: str) -> str:
    return "".join("\\" + ch if ch in '\\"$`' else ch for ch in text)
