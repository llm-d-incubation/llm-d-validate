"""outcome.json -- how a run ended, as the exit code classifies it, plus its counts and failures.

Verification and publication are recorded separately. `publication.status` is only ever set on
the local copy, after the upload; the published copy cannot know how its own upload went.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..http import redact
from ..output.remote import PublicationStatus
from ..profile.known import FIX_EXPECTED, KNOWN, LABELLED, unmatched
from ..results import read as results_file

SCHEMA = "llmdv.outcome/1"
FILENAME = "outcome.json"


MAX_FAILURES = 50
MAX_REASON = 300


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Outcome:
    run_id: str
    labels: dict[str, str] = field(default_factory=dict)
    started: str = field(default_factory=now)
    finished: str | None = None
    profile: dict[str, str | None] = field(default_factory=dict)
    # Which ingress fronts which (profile/topology.py), so the alert feed can attribute a
    # failure without the profile. None until the profile has loaded.
    topology: dict[str, Any] | None = None
    exit_code: int | None = None
    exit_class: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    skips: dict[str, int] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    preflight_problems: list[str] = field(default_factory=list)
    # Local ingresses whose connection was lost mid-run: see connection_lost() below.
    connection_lost: list[dict[str, Any]] = field(default_factory=list)
    # Failures the profile's `known_failures` cover, by issue, and the ones whose fix should
    # already have landed. A subset of the failures above, never a deduction from them.
    known: dict[str, Any] = field(default_factory=dict)
    publication: dict[str, Any] = field(default_factory=dict)
    # What the traffic said about each layer: see evidence/stack.py. Filled when the run ends.
    stack: dict[str, Any] = field(default_factory=dict)
    partial_evidence: int = 0

    def record_results(self, records: list[dict[str, Any]], entries: Iterable = ()) -> None:
        """Fold results.jsonl records into counts, skip classes and failures. `entries` are the
        profile's known_failures, so the ones that labelled nothing can be named."""
        self.counts, self.skips, self.failures = summarise(records)
        self.known = known_summary(records, entries)
        self.connection_lost = connection_lost(records)
        self.partial_evidence = sum(1 for record in records if record.get("partial_evidence"))

    def to_dict(self) -> dict[str, Any]:
        body = asdict(self)
        verification = {
            "exit_code": body.pop("exit_code"),
            "class": body.pop("exit_class"),
            "counts": body.pop("counts"),
            "skips": body.pop("skips"),
            "failures": body.pop("failures"),
            "known": body.pop("known"),
            "partial_evidence": body.pop("partial_evidence"),
        }
        return {
            "schema": SCHEMA,
            "llmdv_version": __version__,
            "git_sha": os.environ.get("LLMDV_GIT_SHA") or None,
            **body,
            "verification": verification,
        }

    def write(self, run_dir: Path) -> None:
        write_dict(run_dir, self.to_dict())


def summarise(records: list[dict[str, Any]]) -> tuple[dict, dict, list]:
    """Counts by outcome, skips by class, and the failures, from results.jsonl records."""
    counts: dict[str, int] = {}
    skips: dict[str, int] = {}
    failures: list[dict[str, Any]] = []
    for record in records:
        outcome = record["outcome"]
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == "skip":
            skip_class = record.get("skip_class") or "unclassified"
            skips[skip_class] = skips.get(skip_class, 0) + 1
        elif outcome in ("fail", "error") and len(failures) < MAX_FAILURES:
            failures.append(
                {
                    "case_id": record.get("case_id"),
                    "variant": record.get("variant"),
                    "ingress": record.get("ingress"),
                    "reason": (record.get("reason") or "")[:MAX_REASON],
                    **_known_fields(record),
                    **(
                        {"connection_lost": record["connection_lost"]}
                        if record.get("connection_lost")
                        else {}
                    ),
                }
            )
    return counts, skips, failures


def connection_lost(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each local ingress whose connection was lost mid-run, and when, in the order it happened.

    `cases_before` is how many cases had finished before the one that found it gone, and
    `cases_skipped` how many later cases were not run because of it.
    """
    found: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        ingress = record.get("connection_lost")
        if not ingress:
            continue
        if record.get("outcome") == "error" and ingress not in found:
            found[ingress] = {
                "ingress": ingress,
                "case_id": record.get("case_id"),
                "variant": record.get("variant"),
                "at": record.get("ts"),
                "cases_before": index,
                "cases_skipped": 0,
            }
        elif record.get("outcome") == "skip" and ingress in found:
            found[ingress]["cases_skipped"] += 1
    return list(found.values())


def environment_only(records: list[dict[str, Any]]) -> bool:
    """Whether every failed or errored case lost its local connection rather than failing.

    True means the run tested nothing it could blame on the fleet: exit 3, not 1.
    """
    failed = [record for record in records if record.get("outcome") in ("fail", "error")]
    return bool(failed) and all(record.get("connection_lost") for record in failed)


def _known_fields(record: dict[str, Any]) -> dict[str, Any]:
    """The label on a failure entry, in the same shape the record carries it."""
    known = record.get("known_failure") or {}
    if known.get("status") not in LABELLED:
        return {}
    return {"known_failure": {"status": known["status"], "issue": known.get("issue")}}


def known_summary(records: list[dict[str, Any]], entries: Iterable = ()) -> dict[str, Any]:
    """How many failures the profile had already filed, by issue (most first), how many it
    expected fixed by now, and which entries labelled nothing. `failing` is part of the fail
    and error counts, not in addition to them."""
    failing = 0
    fix_expected = 0
    issues: dict[str, int] = {}
    for record in records:
        known = record.get("known_failure") or {}
        status = known.get("status")
        if status == KNOWN:
            failing += 1
            issue = str(known.get("issue") or "?")
            issues[issue] = issues.get(issue, 0) + 1
        elif status == FIX_EXPECTED:
            fix_expected += 1
    return {
        "failing": failing,
        "fix_expected": fix_expected,
        "issues": dict(sorted(issues.items(), key=lambda kv: (-kv[1], kv[0]))),
        "unmatched": unmatched(entries, records),
    }


def read_results(run_dir: Path, refused: list[str] | None = None) -> list[dict[str, Any]]:
    """The records results.jsonl holds so far; a crashed run may have cut its last line short.
    Raises `results.read.Unreadable` for a line that is not a result or is written under a
    results schema this llmdv cannot read; with `refused`, describes it there and leaves it out."""
    return results_file.read(Path(run_dir) / "results.jsonl", refused)


def write_dict(run_dir: Path, body: dict[str, Any]) -> None:
    """Replace outcome.json atomically, so a run killed mid-write never leaves it truncated.

    Refuses a publication status outside PublicationStatus: a reader that does not know a
    status cannot tell a published run from a lost one.
    """
    status = (body.get("publication") or {}).get("status")
    if status is not None:
        PublicationStatus(status)
    path = Path(run_dir) / FILENAME
    temporary = path.with_name(f".{FILENAME}.tmp")
    temporary.write_text(json.dumps(redact.redact_value(body), indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load(run_dir: Path) -> Any:
    try:
        return json.loads((Path(run_dir) / FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# The parts of an outcome every reader walks into, and the JSON type each must have.
_MAPPINGS = ("profile", "labels", "verification", "publication")
_VERIFICATION = {"counts": dict, "skips": dict, "failures": list}


def _shape_problem(body: Any) -> str | None:
    if not isinstance(body, dict):
        return "it is not a JSON object"
    for name in _MAPPINGS:
        if body.get(name) is not None and not isinstance(body[name], dict):
            return f"`{name}` is not an object"
    verification = body.get("verification") or {}
    for name, kind in _VERIFICATION.items():
        if verification.get(name) is not None and not isinstance(verification[name], kind):
            return f"`verification.{name}` is not {'an object' if kind is dict else 'a list'}"
    if not all(isinstance(failure, dict) for failure in verification.get("failures") or []):
        return "`verification.failures` holds an entry that is not an object"
    lost = body.get("connection_lost")
    if lost is not None and not (
        isinstance(lost, list) and all(isinstance(entry, dict) for entry in lost)
    ):
        return "`connection_lost` is not a list of objects"
    problems = body.get("preflight_problems")
    if problems is not None and not (
        isinstance(problems, list) and all(isinstance(problem, str) for problem in problems)
    ):
        return "`preflight_problems` is not a list of strings"
    return None


def malformed(run_dir: Path) -> str | None:
    """Why outcome.json is valid JSON yet not an outcome, naming the file; None when it is one,
    is missing, or was cut short (a killed run, which is not the reader's mistake)."""
    body = _load(run_dir)
    if body is None:
        return None
    problem = _shape_problem(body)
    return f"{Path(run_dir) / FILENAME} is not a usable outcome: {problem}" if problem else None


def read(run_dir: Path) -> dict[str, Any] | None:
    """The outcome a run left behind, or None when there is none or it cannot be read: missing,
    cut short, or not shaped as an outcome (`malformed` says why)."""
    body = _load(run_dir)
    return body if _shape_problem(body) is None else None
