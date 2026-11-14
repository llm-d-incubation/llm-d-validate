"""alerts.json -- a finished run as an alerting system reads it.

`outcome.json` says how a run ended and `comparison.json` what changed since the runs before it.
An alerting consumer needs both, joined per case, plus the reading a person makes from the
per-layer table: which layer a failure points at. This file is that join, so a consumer never
re-derives llmdv's verdicts or its attribution, and never needs the profile.

Policy stays out. How severe a regression is and whose it is depend on the fleet and the team
that runs it, so the consumer decides both from the facts here.

Three fields carry the weight:

`fleet_observed` is true only when verification reached a verdict about the fleet: exit class
`ok` or `fleet`. Any other run says nothing about the fleet. A failed preflight is the vantage
point's problem and an interrupted run is partial, and reading their failures as fleet findings
is the mistake the exit codes exist to prevent.

`transition` comes from `llmdv compare`, never from this run alone. A case failing tonight that
failed in the window before it is `failing`, not news. Without a comparison every verdict is
`unbaselined`, which a consumer must not read as a regression. A failure the profile's
`known_failures` had filed is `known`, and carries `known_failure` with the issue whether or
not a comparison was made, so a pager can tell it from a new break on the first night.

`points_at` is read off the declared chain, outermost first, the way the run summary's
attribution ladder reads it: the innermost failing layer whose next layer in passed, or else the
innermost layer that ran the case. It is a hypothesis; `basis` says what it rests on.

`publication` and `job_exit_code` are only set on the local copy, which `llmdv job` also prints as
the last line of its output. Like `outcome.json`, the published copy cannot know how its own
upload went.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..compare.diff import key
from ..compare.report import COMPARISON_JSON
from ..http import redact
from ..profile.schema import depth
from . import outcome

SCHEMA = "llmdv.alerts/1"
FILENAME = "alerts.json"

# Verification classes that observed the fleet. Every other class blames something else.
FLEET_OBSERVED = frozenset({"ok", "fleet"})
_DECIDED = frozenset({"pass", "fail", "error"})
_BAD = frozenset({"fail", "error"})

MAX_RESULTS = 1000
MAX_REASON = 200


def build(run_dir: Path) -> dict[str, Any] | None:
    """The feed for a finished run directory, or None when the run left no outcome."""
    body = outcome.read(run_dir)
    if body is None:
        return None
    records = [r for r in outcome.read_results(run_dir) if r.get("outcome") in _DECIDED]
    verification = body.get("verification") or {}
    comparison = _read_json(Path(run_dir) / COMPARISON_JSON)
    made = bool(comparison and comparison.get("made"))
    transitions = {
        (str(t["ingress"]), str(t["case_id"]), str(t["variant"])): t["transition"]
        for t in (comparison or {}).get("transitions") or []
    }

    columns: dict[tuple[str, str], dict[str, dict]] = {}
    for record in records:
        ingress, case, variant = key(record)
        columns.setdefault((case, variant), {})[ingress] = record

    results = []
    for record in records:
        ingress, case, variant = key(record)
        entry = {
            "case_id": case,
            "variant": variant or None,
            "ingress": ingress,
            "outcome": record["outcome"],
            "transition": transitions.get((ingress, case, variant), "unbaselined"),
        }
        if record["outcome"] in _BAD:
            points_at, basis = attribute(record, columns[(case, variant)])
            reason = (record.get("reason") or "").strip().splitlines()
            entry.update(
                ingress_kind=record.get("ingress_kind"),
                surface=record.get("surface"),
                points_at=points_at.get("ingress"),
                points_at_kind=points_at.get("ingress_kind"),
                basis=basis,
                reason=reason[0][:MAX_REASON] if reason else None,
                evidence=record.get("evidence"),
                known_failure=_known(record),
            )
        results.append(entry)
    # Failures first, so a cap can only ever drop passes: a dropped pass leaves a finding open,
    # where a dropped failure would read as health.
    results.sort(key=lambda e: e["outcome"] not in _BAD)

    return {
        "schema": SCHEMA,
        "run_id": body.get("run_id"),
        "profile": (body.get("profile") or {}).get("name"),
        "profile_digest": next(
            (r["profile_digest"] for r in records if r.get("profile_digest")), None
        ),
        "started": body.get("started"),
        "finished": body.get("finished"),
        "labels": body.get("labels") or {},
        "verification": {
            "exit_code": verification.get("exit_code"),
            "class": verification.get("class"),
            "counts": verification.get("counts") or {},
        },
        "fleet_observed": verification.get("class") in FLEET_OBSERVED,
        "preflight_problems": [p.splitlines()[0] for p in body.get("preflight_problems") or []][:5],
        "comparison": _comparison(comparison, made),
        "results": results[:MAX_RESULTS],
        "truncated": max(0, len(results) - MAX_RESULTS),
    }


def attribute(record: dict, column: dict[str, dict]) -> tuple[dict, str]:
    """The layer a failing result points at, and the observation that points there.

    `column` holds the same case's decided results by ingress. Only layers strictly inside this
    one count, nearest first: two layers of one kind have no declared order between them.
    """
    here = record.get("ingress") or "-"
    mine = _depth(record)

    def deeper(other: dict) -> bool:
        theirs = _depth(other)
        return mine is not None and theirs is not None and theirs > mine

    inner = sorted(filter(deeper, column.values()), key=_depth)
    culprit = record
    for other in inner:
        if other["outcome"] in _BAD:
            culprit = other
            continue
        return culprit, f"fails at {culprit.get('ingress')}, passes at {other.get('ingress')}"
    where = culprit.get("ingress")
    if where == here:
        return culprit, f"fails at {where}, the innermost layer that ran this case"
    return culprit, f"fails at {where}, the innermost layer that ran this case, and at {here}"


def write(run_dir: Path, feed: dict[str, Any]) -> None:
    """Replace alerts.json atomically, like outcome.json."""
    path = Path(run_dir) / FILENAME
    temporary = path.with_name(f".{FILENAME}.tmp")
    temporary.write_text(json.dumps(redact.redact_value(feed), indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def line(feed: dict[str, Any]) -> str:
    """The feed as one line, for a reader that takes it from the job log."""
    return json.dumps(feed, separators=(",", ":"))


def _comparison(comparison: dict | None, made: bool) -> dict[str, Any]:
    if comparison is None:
        return {"made": False, "reason": "no comparison was requested"}
    if not made:
        return {"made": False, "reason": comparison.get("reason") or "unknown"}
    return {
        "made": True,
        "baseline": comparison.get("baseline"),
        "window": comparison.get("window"),
        "profile_changed": bool(comparison.get("profile_changed")),
    }


def _depth(record: dict) -> int | None:
    try:
        return depth(record.get("ingress_kind") or "")
    except (KeyError, ValueError):
        return None


def _read_json(path: Path) -> dict | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _known(record: dict[str, Any]) -> dict[str, Any] | None:
    known = record.get("known_failure") or {}
    if not known.get("status"):
        return None
    return {"status": known["status"], "issue": known.get("issue"), "fix": known.get("fix")}
