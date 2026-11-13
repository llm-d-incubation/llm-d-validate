"""Known failures: a label for a failure the team has filed and traced to a pending fix.

A profile declares what a correct stack does, and a case that fails stays failed. But a fleet
that is waiting on an upstream release fails the same cases every night, and a reader cannot
tell those from a new break. A `known_failures` entry links the first failing check of a case to
the finding and the fix, so the run can say "failed, known, fix pending" without softening the
verdict: the outcome stays `fail`, the exit code stays `1`, and a case failing a check the entry
does not name is a plain failure.

An entry may also say when the fix is expected to have landed, as build signals the run already
records (`evidence/stack.py`). A response carrying every named signal at or past its value turns
the known failure back into a plain one, reported as `fix_expected`: the fix did not take, and
the entry must not hide that.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .schema import KnownFailure

KNOWN = "known"
FIX_EXPECTED = "fix_expected"
LABELLED = (KNOWN, FIX_EXPECTED)

_VERSION = re.compile(r"\d+(?:\.\d+)+")

# (the ingress a response came from, the build signals it carried)
Signal = tuple[str | None, Mapping[str, str]]


def first_failed_check(checks: Iterable[Mapping[str, Any]]) -> str | None:
    """The name of the first named check recorded not-ok: the key failure groups are built on.
    A failed check with no name cannot be declared, so it is passed over."""
    for check in checks:
        if check.get("ok") is False and check.get("name"):
            return str(check["name"])
    return None


def ordered(entries: Iterable[KnownFailure]) -> list[KnownFailure]:
    """Most specific first, so a broad entry never shadows one that names the case or the
    layer. Ties keep profile order."""
    return sorted(entries, key=lambda e: -e.specificity)


def classify(
    entries: Iterable[KnownFailure],
    *,
    outcome: str,
    case_id: str | None,
    variant: str | None,
    ingress: str | None,
    checks: Iterable[Mapping[str, Any]],
    signals: Iterable[Signal],
) -> dict[str, Any] | None:
    """What the profile says about this result, or None when it says nothing.

    Only a failure is ever labelled: the most specific entry naming its first failing check,
    with status `known`, or `fix_expected` when responses carried the build the entry waits
    for. A pass is left alone; a comparison reads the previous run's label to say a fix landed.
    """
    if outcome not in ("fail", "error"):
        return None
    failed = first_failed_check(checks)
    if failed is None:
        return None
    signals = list(signals)
    for entry in ordered(entries):
        if entry.names_check(failed) and entry.covers(case_id, variant, ingress):
            landed = _landed(entry, signals)
            return {
                "status": FIX_EXPECTED if landed else KNOWN,
                "check": failed,
                "issue": entry.issue,
                "fix": entry.fix,
                "until": dict(entry.until),
                "observed": landed or {},
            }
    return None


def _landed(entry: KnownFailure, signals: list[Signal]) -> dict[str, str]:
    """The observed value of every awaited signal when all of them are at or past the entry's
    values, else empty. Only responses from the entry's own ingresses count, so a gateway
    header can never speak for the engine behind it."""
    if not entry.until:
        return {}
    seen: dict[str, str] = {}
    for name, wanted in entry.until.items():
        for from_ingress, carried in signals:
            if entry.ingresses and from_ingress not in entry.ingresses:
                continue
            observed = carried.get(name)
            if observed is not None and at_or_past(observed, wanted):
                seen[name] = observed
                break
        else:
            return {}
    return seen


def at_or_past(observed: str, wanted: str) -> bool:
    """Whether a build signal is the awaited value or a later version of it.

    Equal strings always match. Otherwise `wanted` must hold a dotted number, and the number
    compared in `observed` is the first one after the same literal prefix: `Python/3.13`
    finds the number after `Python/` in `BaseHTTP/0.6 Python/3.12.3`, and `1.104.1` the first
    number of `1.104.1`. A value with no dotted number, such as a commit fingerprint, matches
    only by equality.
    """
    if observed == wanted:
        return True
    target = _VERSION.search(wanted)
    if not target:
        return False
    prefix = wanted[: target.start()]
    start = 0
    if prefix:
        start = observed.find(prefix)
        if start < 0:
            return False
        start += len(prefix)
    ours = _VERSION.match(observed, start) if prefix else _VERSION.search(observed)
    if not ours:
        return False
    return _parts(ours.group()) >= _parts(target.group())


def _parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def matches_case(patterns: Iterable[str], case_id: str | None, variant: str | None) -> bool:
    """Whether any glob names the case, by id or by `id[variant]`.

    Only `*` and `?` are wild. Brackets are literal, because a variant is written in them
    (`openai_chat.request_field[logit_bias]`) and fnmatch would read that as a character class.
    """
    names = [case_id or ""]
    if variant:
        names.append(f"{case_id}[{variant}]")
    return any(_glob(pattern).fullmatch(name) for pattern in patterns for name in names)


def _glob(pattern: str) -> re.Pattern[str]:
    parts = [
        ".*" if piece == "*" else "." if piece == "?" else re.escape(piece)
        for piece in re.split(r"([*?])", pattern)
    ]
    return re.compile("".join(parts))


def unmatched(entries: Iterable[KnownFailure], records: Iterable[Mapping[str, Any]]) -> list[dict]:
    """The entries that labelled no record: candidates for pruning, or for a `-k` that left
    their cases out. Matched by issue and check, which together name an entry."""
    used = {
        (str(k.get("issue")), str(k.get("check")))
        for r in records
        if (k := r.get("known_failure")) and k.get("status") in LABELLED
    }
    return [
        {"check": e.check, "issue": e.issue}
        for e in entries
        if not any(issue == e.issue and e.names_check(check) for issue, check in used)
    ]
