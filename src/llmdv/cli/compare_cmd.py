"""`llmdv compare` -- what changed between two runs of the same profile.

A run answers "does this fleet hold now". It cannot answer "did it get worse", because nothing
it writes is compared to anything earlier. This reads two runs back out of the archive that
`--remote-output` wrote and says which cases changed their answer.

Exit code is the point of the command as much as the text: a regression exits 1, so a scheduled
job can gate on it without parsing anything.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..compare.archive import (
    PROFILE_FILE,
    ArchiveUnreadable,
    EmptyRun,
    InvalidRun,
    NoSuchRun,
    UnsafeName,
    declaration_diff,
    list_runs,
    pick_pair,
    pick_window,
    publish_one,
    read_identity,
    read_local_results,
    read_outcome,
    read_profile,
    read_results,
    read_stack,
    require_records,
)
from ..compare.diff import Delta, attach_stack, compare_window
from ..compare.report import COMPARISON_FILE, COMPARISON_JSON, as_record, not_made, render
from ..evidence import outcome
from ..evidence.stack import declared_in_both, stack_from_run, version_headers_in
from ..http.redact import scrub
from ..identity import CONFIGURATION_DIMENSIONS, differences
from ..log.setup import emit, get_logger
from ..output.remote import UnsupportedRemote, missing_credentials
from .exit_codes import OK, PREFLIGHT, TEST_FAILURES, USAGE

log = get_logger("cli.compare")


def _identity_notes(before: dict | None, after: dict | None) -> list[str]:
    return [
        f"identity {item}"
        for item in differences(before, after)
        if item.split(":", 1)[0] not in CONFIGURATION_DIMENSIONS
    ]


def add_parser(subparsers, parents) -> None:
    command = subparsers.add_parser(
        "compare",
        parents=parents,
        help="report what changed between two runs of the same profile",
    )
    command.add_argument(
        "--from",
        dest="source",
        required=True,
        help="where runs were published: s3://bucket/prefix or file:///path",
    )
    command.add_argument("--profile", required=True, help="profile name, as the run recorded it")
    command.add_argument(
        "--baseline",
        default=None,
        help="run id to compare the latest against; default is the run before it",
    )
    command.add_argument(
        "--window",
        type=int,
        default=1,
        help="how many earlier runs must agree before a change is called a regression "
        "(default 1: the most recent run). A larger window tolerates a flaky case -- one the "
        "window disagrees about is reported as flaky rather than as a regression.",
    )
    command.add_argument(
        "--list",
        action="store_true",
        help="list the runs available for this profile and stop",
    )
    command.add_argument(
        "--no-fail",
        action="store_true",
        help="exit 0 even when something regressed, for a notify-only job",
    )
    command.set_defaults(func=run)


def compare_run(
    run_dir: Path, source: str, profile: str, baseline: str | None = None, window: int = 1
) -> tuple[int, str]:
    """Compare local results against the archive without risking the job's delivery steps.

    Reading, analysis and rendering share one exception boundary. A failed comparison yields
    no verdict, but leaves its reason in comparison.md for publication and notification, and in
    comparison.json so a machine reader can tell it from a comparison that found nothing.
    """
    try:
        return _compare_run(run_dir, source, profile, baseline, window)
    except Exception as exc:  # noqa: BLE001 -- advisory; never cost publication or notification
        record = (
            log.info if isinstance(exc, (NoSuchRun, EmptyRun, UnsupportedRemote)) else log.warning
        )
        record("no comparison made: %s", exc)
        reason = " ".join(scrub(str(exc)).split()) or type(exc).__name__
        text = f"Comparison unavailable: {reason}"
    _save_comparison(
        run_dir,
        source,
        profile,
        text,
        not_made(reason, (outcome.read(run_dir) or {}).get("identity")),
    )
    return OK, text


def _compare_run(
    run_dir: Path, source: str, profile: str, baseline: str | None, window: int
) -> tuple[int, str]:
    # The finishing run may not have reached the archive. Always judge its local results,
    # and only use the archive to select a completely published baseline.
    after = require_records(read_local_results(run_dir), run_dir.name)
    runs = list_runs(source, profile)
    current_identity = (outcome.read(run_dir) or {}).get("identity")
    if baseline:
        chosen = pick_window(runs, exclude=_run_id(after), baseline=baseline, size=window)
    else:
        chosen = []
        for candidate in reversed(runs):
            if candidate == _run_id(after):
                continue
            candidate_identity = read_identity(source, profile, candidate)
            if (
                candidate_identity
                and isinstance(current_identity, dict)
                and all(
                    candidate_identity.get(key) == current_identity.get(key)
                    for key in CONFIGURATION_DIMENSIONS
                )
            ):
                chosen.append(candidate)
                if len(chosen) == max(1, window):
                    break
        if not chosen:
            earlier = [candidate for candidate in runs if candidate != _run_id(after)]
            if not earlier:
                raise NoSuchRun("the archive holds no earlier run of this profile")
            if not isinstance(current_identity, dict):
                raise NoSuchRun("this run recorded no identity to match a baseline by")
            raise NoSuchRun("no earlier run has this run's configuration identity")
        chosen.reverse()
    if baseline:
        baseline_identity = read_identity(source, profile, chosen[0])
        if isinstance(current_identity, dict) and baseline_identity:
            mismatch = [
                key
                for key in CONFIGURATION_DIMENSIONS
                if current_identity.get(key) != baseline_identity.get(key)
            ]
            if mismatch:
                raise NoSuchRun("baseline identity differs in: " + ", ".join(mismatch))
    delta, before_id = _compare_baselines(
        source, profile, chosen, after, (outcome.read(run_dir) or {}).get("selection")
    )
    local = run_dir / PROFILE_FILE
    before_profile = read_profile(source, profile, before_id)
    after_profile = local.read_text(encoding="utf-8") if local.is_file() else None
    if delta.digest_changed:
        # Say WHICH declaration moved. One tightened line can account for every case that
        # changed, and without it a reader has to diff two profiles by hand.
        delta.profile_diff = declaration_diff(before_profile, after_profile)

    # Configuration-compatible runs may still differ in provenance or the profile itself.
    # Keep those differences visible in standalone comparisons as well as job comparisons.
    before_identity = read_identity(source, profile, before_id)
    notes = _identity_notes(before_identity, current_identity)

    # The finishing run's stack from its own outcome, or from its exchanges when the outcome
    # was written by a llmdv that did not record one.
    local_stack = (outcome.read(run_dir) or {}).get("stack") or stack_from_run(run_dir)
    attach_stack(
        delta,
        read_stack(source, profile, before_id),
        local_stack,
        declared_in_both(before_profile, after_profile),
        version_headers_in(after_profile),
    )

    after_id = _run_id(after)
    text = render(delta, profile, before_id, after_id or run_dir.name)
    if notes:
        text += "\nNotes:\n" + "\n".join(f"  • {note}" for note in notes)
    data = as_record(delta, before_id, after_id or run_dir.name, current_identity, notes)
    _save_comparison(run_dir, source, profile, text, data, after_id)
    log.info("compared against %s", before_id)
    return (TEST_FAILURES if delta.regressed else OK), text


def _compare_baselines(
    source: str,
    profile: str,
    chosen: list[str],
    after: list[dict],
    selection: dict | None = None,
) -> tuple[Delta, str]:
    """Discard unusable archived runs whole; tonight's results remain strict.

    `selection` is the later run's recorded selection. Each baseline's is read from its
    outcome, so a case one run did not select is not reported as the suite changing.
    """
    history, errors, unreadable = {}, {}, False
    for run_id in chosen:
        try:
            history[run_id] = require_records(read_results(source, profile, run_id), run_id)
        except (InvalidRun, EmptyRun, NoSuchRun, ArchiveUnreadable) as exc:
            errors[run_id] = " ".join(scrub(str(exc)).split())
            unreadable = unreadable or isinstance(exc, ArchiveUnreadable)
    if not history:
        # One baseline the store would not hand over makes this the store's failure, not a
        # statement about the runs.
        kind = ArchiveUnreadable if unreadable else NoSuchRun
        raise kind("no usable baseline runs: " + "; ".join(errors.values()))
    selections = [_selection(source, profile, run_id) for run_id in history]
    delta = compare_window(list(history.values()), after, selections, selection)
    delta.window_errors = errors
    delta.window_dropped += len(errors)
    # The run the verdicts were judged against, which after a reverted profile is not the
    # newest: its stack, profile and digest are the ones the report must name.
    ids = list(history)
    before_id = ids[delta.baseline] if delta.baseline is not None else ids[-1]
    return delta, before_id


def _selection(source: str, profile: str, run_id: str) -> dict | None:
    """A run's recorded `selection`, or None when it recorded none or its outcome is unreadable.

    Advisory: without it a case the run left out reads as suite churn, never as a verdict.
    """
    try:
        recorded = (read_outcome(source, profile, run_id) or {}).get("selection")
    except ArchiveUnreadable:
        return None
    return recorded if isinstance(recorded, dict) else None


def _save_comparison(
    run_dir: Path, source: str, profile: str, text: str, data: dict, run_id: str | None = None
) -> None:
    for name, body in ((COMPARISON_FILE, text), (COMPARISON_JSON, json.dumps(data, indent=2))):
        written = run_dir / name
        try:
            written.write_text(body + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("could not write %s: %s", name, exc)
            continue
        # The run was uploaded by the verify child before this ran, so the comparison has to be
        # sent on its own or it never leaves the pod. Best effort: the analysis is also in the
        # notification, and losing the copy must not fail the job.
        try:
            # Malformed results cannot supply the id, and the job directory has a different
            # name. outcome.json still identifies the bundle that verify already published.
            run_id = run_id or (outcome.read(run_dir) or {}).get("run_id")
            if run_id:
                log.info("published %s to %s", name, publish_one(source, profile, run_id, written))
        except Exception as exc:  # noqa: BLE001 -- advisory, never fatal
            log.warning("could not publish %s: %s", name, exc)


def _run_id(records: list[dict]) -> str | None:
    for record in records:
        if record.get("run_id"):
            return str(record["run_id"])
    return None


def run(args: argparse.Namespace) -> int:
    try:
        reason = missing_credentials(args.source)
        if reason is None:
            runs = list_runs(args.source, args.profile)
    except (UnsupportedRemote, UnsafeName) as exc:
        log.error("%s", exc)
        return USAGE
    except ArchiveUnreadable as exc:
        # The store, its endpoint or the credentials: the operator's environment.
        log.error("%s", exc)
        return PREFLIGHT
    if reason is not None:
        log.error("cannot read runs from %s: %s", args.source, reason)
        return PREFLIGHT

    if args.list:
        if not runs:
            log.error("no runs found for %r under %s", args.profile, args.source)
            return USAGE
        emit(f"  {len(runs)} run(s) for {args.profile}, oldest first:")
        for name in runs:
            emit(f"    {name}")
        return OK

    try:
        if args.baseline:
            before_id, after_id = pick_pair(runs, args.baseline)
        else:
            after_id = runs[-1] if runs else None
            after_identity = (
                read_identity(args.source, args.profile, after_id) if after_id else None
            )
            if not after_identity:
                raise NoSuchRun("latest run has no identity; name an older run explicitly")
            compatible = [
                run_id
                for run_id in runs[:-1]
                if (candidate := read_identity(args.source, args.profile, run_id))
                and all(
                    candidate.get(key) == after_identity.get(key)
                    for key in CONFIGURATION_DIMENSIONS
                )
            ]
            if not compatible:
                raise NoSuchRun("no earlier run has the latest run's configuration identity")
            before_id, after_id = compatible[-1], after_id
        after = require_records(read_results(args.source, args.profile, after_id), after_id)
        chosen = pick_window(runs, exclude=after_id, baseline=args.baseline, size=args.window)
        if not args.baseline:
            chosen = compatible[-max(1, args.window) :]
        if args.baseline:
            before_identity = read_identity(args.source, args.profile, before_id)
            after_identity = read_identity(args.source, args.profile, after_id)
            if before_identity and after_identity:
                mismatch = [
                    key
                    for key in CONFIGURATION_DIMENSIONS
                    if before_identity.get(key) != after_identity.get(key)
                ]
                if mismatch:
                    raise NoSuchRun("baseline identity differs in: " + ", ".join(mismatch))
        delta, before_id = _compare_baselines(
            args.source,
            args.profile,
            chosen,
            after,
            _selection(args.source, args.profile, after_id),
        )
    except (NoSuchRun, EmptyRun, InvalidRun, UnsafeName) as exc:
        # Not having a comparison is a usage answer, never a passing one.
        log.error("%s", exc)
        return USAGE
    except ArchiveUnreadable as exc:
        # A run that is there and cannot be read: the store or its permissions.
        log.error("%s", exc)
        return PREFLIGHT

    try:
        before_profile = read_profile(args.source, args.profile, before_id)
        after_profile = read_profile(args.source, args.profile, after_id)
        if delta.digest_changed:
            delta.profile_diff = declaration_diff(before_profile, after_profile)
        attach_stack(
            delta,
            read_stack(args.source, args.profile, before_id),
            read_stack(args.source, args.profile, after_id),
            declared_in_both(before_profile, after_profile),
            version_headers_in(after_profile),
        )

        notes = _identity_notes(
            read_identity(args.source, args.profile, before_id),
            read_identity(args.source, args.profile, after_id),
        )
    except ArchiveUnreadable as exc:
        log.error("%s", exc)
        return PREFLIGHT

    emit("")
    for line in render(delta, args.profile, before_id, after_id).splitlines():
        emit(f"  {line}")
    if notes:
        emit("  Notes:")
        for note in notes:
            emit(f"    • {note}")
    emit("")

    if delta.regressed and not args.no_fail:
        return TEST_FAILURES
    return OK
