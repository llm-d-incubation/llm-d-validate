"""Slack notification for a finished run, built from its outcome.json.

Delivery is retried within bounds; test requests, by contrast, never are.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from . import __version__
from .cli.exit_codes import CRASHED_MEANING, EXITS, MEANING, class_of

TIMEOUT = 10.0
MAX_ATTEMPTS = 4
# Retry-After is honoured up to this; a longer one stops delivery, as the run's time is finite.
MAX_WAIT = 30.0
MAX_FAILURES_SHOWN = 10

# The status word for each exit class. What the code means is said in the exit table's own
# words (cli/exit_codes.py), so the notification cannot drift from the manual.
STATUS = {
    "ok": "PASSED",
    "fleet": "FAILED",
    "environment": "NOT RUN",
    "usage": "NOT RUN",
    "internal": "ERROR",
    "interrupted": "INTERRUPTED",
    "crashed": "CRASHED",
}
# An environment problem that struck after cases had run: a local connection lost mid-run.
INCOMPLETE = "INCOMPLETE"


class NotifyError(Exception):
    pass


def _escape(text: str) -> str:
    """Slack treats &, < and > as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _headline(klass: str | None, code: int | None, ran: bool = False) -> str:
    """The status word and the table's meaning. A class that is missing or unknown is read from
    the code, so a run that died without a verdict is crashed, never an llmdv defect."""
    if klass not in STATUS:
        klass = class_of(code)
    meaning = MEANING.get(code) or next(
        (text for name, text in EXITS.values() if name == klass), CRASHED_MEANING
    )
    status = INCOMPLETE if klass == "environment" and ran else STATUS[klass]
    return f"{status}, {meaning} (exit {code})"


def message(
    outcome: dict[str, Any] | None,
    exit_code: int | None = None,
    comparison: str | None = None,
) -> str:
    """The Slack text for a run: plain lines, no formatting."""
    if outcome is None:
        return (
            f"llmdv: {_headline(None, exit_code)}\n"
            "The run ended before recording an outcome, so no results are available. "
            "See the job log."
        )

    verification = outcome.get("verification") or {}
    code = verification.get("exit_code")
    if code is None:
        code = exit_code
    counts = verification.get("counts") or {}
    lost = outcome.get("connection_lost") or []
    ran = bool(lost) or any(counts.values())
    profile = (outcome.get("profile") or {}).get("name") or "?"
    lines = [f"llmdv {_escape(profile)}: {_headline(verification.get('class'), code, ran)}"]

    labels = outcome.get("labels") or {}
    if labels:
        lines.append(", ".join(f"{key}={_escape(value)}" for key, value in labels.items()))

    if counts:
        skips = verification.get("skips") or {}
        skip_detail = ", ".join(f"{count} {name}" for name, count in sorted(skips.items()))
        failed = counts.get("fail", 0) + counts.get("error", 0)
        lines.append(
            f"passed {counts.get('pass', 0)}, failed {failed}, skipped {counts.get('skip', 0)}"
            + (f" ({skip_detail})" if skip_detail else "")
        )

    for entry in lost:
        lines.append(
            f"- connection to {_escape(str(entry.get('ingress') or '?'))} lost at "
            f"{_escape(str(entry.get('case_id') or '?'))} after {entry.get('cases_before', 0)} "
            f"case(s); {entry.get('cases_skipped', 0)} later case(s) not run"
        )

    for problem in (outcome.get("preflight_problems") or [])[:3]:
        lines.append(f"- {_escape(problem.splitlines()[0])}")

    failures = verification.get("failures") or []
    for failure in failures[:MAX_FAILURES_SHOWN]:
        case = failure.get("case_id") or "?"
        if failure.get("variant"):
            case += f"[{failure['variant']}]"
        reason = (failure.get("reason") or "").splitlines()
        lines.append(
            f"- {_escape(case)} at {_escape(failure.get('ingress') or '-')}"
            + (f": {_escape(reason[0][:150])}" if reason else "")
        )
    if len(failures) > MAX_FAILURES_SHOWN:
        lines.append(f"- and {len(failures) - MAX_FAILURES_SHOWN} more in results.jsonl")

    publication = outcome.get("publication") or {}
    if publication.get("status") == "published":
        lines.append(f"results: {_escape(publication.get('location') or '')}")
        primary = publication.get("primary")
        if primary:
            lines.append(
                f"saved to the fallback: {_escape(primary.get('destination') or '?')} was "
                f"unavailable ({_escape(primary.get('error') or 'unknown error')})"
            )
    elif publication.get("status") == "failed":
        error = _escape(publication.get("error") or "unknown error")
        lines.append(f"results NOT published: {error}")
    elif publication.get("status") == "skipped":
        reason = _escape(publication.get("reason") or "not configured")
        lines.append(f"results: publication skipped ({reason}); artifacts remain local")
    elif publication.get("destination"):
        # A copy from inside a published bundle, which cannot record its own upload's result.
        lines.append(
            f"results: sent to {_escape(publication['destination'])} "
            "(this copy does not record whether the upload completed)"
        )
    else:
        lines.append("results: not published (no --remote-output)")

    # What changed since the last run, when a comparison was made. It goes ABOVE the run
    # footer because it is the part somebody scanning a channel actually needs: "did this get
    # worse" is a different question from "did it pass", and the second is already answered by
    # the headline.
    if comparison:
        lines.append("")
        lines.append(comparison.strip())

    sha = (outcome.get("git_sha") or "")[:7]
    lines.append("")
    lines.append(
        f"run {outcome.get('run_id')}, llmdv {outcome.get('llmdv_version') or __version__}"
        + (f" ({sha})" if sha else "")
    )
    return "\n".join(lines)


def post(url: str, text: str, sleep=time.sleep) -> None:
    """Send `text` to a Slack incoming webhook. Raises NotifyError if it was not delivered.

    The URL is a credential: it never appears in an error raised here.
    """
    last = "no attempt made"
    with httpx.Client(timeout=TIMEOUT) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = client.post(url, json={"text": text})
            except httpx.HTTPError as exc:
                last = f"transport error: {type(exc).__name__}"
            else:
                if response.status_code == 200:
                    return
                last = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code == 429:
                    try:
                        wait = max(float(response.headers.get("Retry-After", "1")), 0.0)
                    except ValueError:
                        wait = 1.0
                    if wait > MAX_WAIT:
                        raise NotifyError(
                            f"Slack asked to retry after {wait:.0f}s, longer than the "
                            f"{MAX_WAIT:.0f}s this run allows; not retried"
                        )
                    if attempt < MAX_ATTEMPTS:
                        sleep(wait)
                    continue
                if response.status_code < 500:
                    raise NotifyError(f"Slack refused the message: {last}")
            if attempt < MAX_ATTEMPTS:
                sleep(min(2.0**attempt, MAX_WAIT))
    raise NotifyError(f"gave up after {MAX_ATTEMPTS} attempts; last: {last}")
