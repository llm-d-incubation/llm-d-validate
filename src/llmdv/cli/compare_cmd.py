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
    NIGHTLY_TRIGGER,
    PROFILE_FILE,
    EmptyRun,
    InvalidRun,
    NoSuchRun,
    declaration_diff,
    list_runs,
    pick_pair,
    pick_window,
    publish_one,
    read_local_results,
    read_profile,
    read_results,
    read_stack,
    read_trigger,
    require_records,
)
from ..compare.diff import Delta, attach_stack, compare_window, digests
from ..compare.report import COMPARISON_FILE, COMPARISON_JSON, as_record, not_made, render
from ..evidence import outcome
from ..evidence.stack import stack_from_run
from ..http.redact import scrub
from ..log.setup import emit, get_logger
from ..output.remote import UnsupportedRemote
from .exit_codes import OK, TEST_FAILURES, USAGE

log = get_logger("cli.compare")


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
    _save_comparison(run_dir, source, profile, text, not_made(reason))
    return OK, text


def _compare_run(
    run_dir: Path, source: str, profile: str, baseline: str | None, window: int
) -> tuple[int, str]:
    # The finishing run may not have reached the archive. Always judge its local results,
    # and only use the archive to select a completely published baseline.
    after = require_records(read_local_results(run_dir), run_dir.name)
    runs = list_runs(source, profile)
    if baseline:
        chosen = pick_window(runs, exclude=_run_id(after), baseline=baseline, size=window)
    else:
        chosen = []
        for candidate in reversed(runs):
            if candidate == _run_id(after):
                continue
            if read_trigger(source, profile, candidate) == NIGHTLY_TRIGGER:
                chosen.append(candidate)
                if len(chosen) == max(1, window):
                    break
        if not chosen:
            raise NoSuchRun("the archive holds no earlier nightly run of this profile")
        chosen.reverse()
    delta, before_id = _compare_baselines(source, profile, chosen, after)
    if delta.digest_changed:
        # Say WHICH declaration moved. One tightened line can account for every case that
        # changed, and without it a reader has to diff two profiles by hand.
        local = run_dir / PROFILE_FILE
        delta.profile_diff = declaration_diff(
            read_profile(source, profile, before_id),
            local.read_text(encoding="utf-8") if local.is_file() else None,
        )

    # The finishing run's stack from its own outcome, or from its exchanges when the outcome
    # was written by a llmdv that did not record one.
    local_stack = (outcome.read(run_dir) or {}).get("stack") or stack_from_run(run_dir)
    attach_stack(delta, read_stack(source, profile, before_id), local_stack)

    after_id = _run_id(after)
    text = render(delta, profile, before_id, after_id or run_dir.name)
    data = as_record(delta, before_id, after_id or run_dir.name)
    _save_comparison(run_dir, source, profile, text, data, after_id)
    log.info("compared against %s", before_id)
    return (TEST_FAILURES if delta.regressed else OK), text


def _compare_baselines(
    source: str, profile: str, chosen: list[str], after: list[dict]
) -> tuple[Delta, str]:
    """Discard unusable archived runs whole; tonight's results remain strict."""
    history, errors = {}, {}
    for run_id in chosen:
        try:
            history[run_id] = require_records(read_results(source, profile, run_id), run_id)
        except (InvalidRun, EmptyRun, NoSuchRun, OSError, UnicodeError) as exc:
            errors[run_id] = " ".join(scrub(str(exc)).split())
    if not history:
        raise NoSuchRun("no usable baseline runs: " + "; ".join(errors.values()))
    delta = compare_window(list(history.values()), after)
    delta.window_errors = errors
    delta.window_dropped += len(errors)
    before_id = next(reversed(history))
    newest = history[before_id]
    delta.digest_changed = bool(
        digests(newest) and digests(after) and digests(newest) != digests(after)
    )
    return delta, before_id


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
        runs = list_runs(args.source, args.profile)
    except UnsupportedRemote as exc:
        log.error("%s", exc)
        return USAGE

    if args.list:
        if not runs:
            log.error("no runs found for %r under %s", args.profile, args.source)
            return USAGE
        emit(f"  {len(runs)} run(s) for {args.profile}, oldest first:")
        for name in runs:
            emit(f"    {name}")
        return OK

    try:
        before_id, after_id = pick_pair(runs, args.baseline)
        after = require_records(read_results(args.source, args.profile, after_id), after_id)
        chosen = pick_window(runs, exclude=after_id, baseline=args.baseline, size=args.window)
        delta, before_id = _compare_baselines(args.source, args.profile, chosen, after)
    except (NoSuchRun, EmptyRun, InvalidRun) as exc:
        # Not having a comparison is a usage answer, never a passing one.
        log.error("%s", exc)
        return USAGE

    if delta.digest_changed:
        delta.profile_diff = declaration_diff(
            read_profile(args.source, args.profile, before_id),
            read_profile(args.source, args.profile, after_id),
        )
    attach_stack(
        delta,
        read_stack(args.source, args.profile, before_id),
        read_stack(args.source, args.profile, after_id),
    )

    emit("")
    for line in render(delta, args.profile, before_id, after_id).splitlines():
        emit(f"  {line}")
    emit("")

    if delta.regressed and not args.no_fail:
        return TEST_FAILURES
    return OK
