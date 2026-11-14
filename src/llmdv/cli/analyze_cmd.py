"""`llmdv analyze` -- build the triage dashboard for a run directory.

The same page `llmdv verify --analyze` writes as a run finishes, built later: for a run made
without the flag, a run fetched back from storage, or a run that was killed. It reads only the
run directory and sends nothing anywhere.

Without `--notes` the page is static: only what llmdv recorded. With `--notes`, an analysis
written elsewhere (by an agent or by a person) is embedded,
labelled as such and kept apart from the recorded evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..evidence import dashboard
from ..log.setup import emit, get_logger
from .exit_codes import NOT_DELIVERED, OK, USAGE

log = get_logger("cli.analyze")


def add_parser(subparsers, parents) -> None:
    command = subparsers.add_parser(
        "analyze",
        parents=parents,
        help="build dashboard.html for a run directory: every test with its evidence",
    )
    command.add_argument("run_dir", type=Path, help="the run directory `llmdv verify` wrote")
    command.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="where to write the page (default: <run_dir>/dashboard.html)",
    )
    command.add_argument(
        "--notes",
        type=Path,
        default=None,
        help="JSON file of analysis notes to embed, shown labelled as not recorded by llmdv: "
        '{"author", "summary", "groups": {check: note}, "cases": {test or case_id: note}, '
        '"intents": {case_id: what the case sets out to prove}}',
    )
    command.set_defaults(func=cmd_analyze)


def cmd_analyze(args: argparse.Namespace) -> int:
    if getattr(args, "pytest_args", None):
        log.error("unrecognised arguments: %s", " ".join(args.pytest_args))
        return USAGE
    if not args.run_dir.is_dir() or not dashboard.is_run_dir(args.run_dir):
        log.error(
            "%s is not a llmdv run directory (no results.jsonl or outcome.json)", args.run_dir
        )
        return USAGE
    notes = None
    if args.notes is not None:
        try:
            notes = dashboard.validate_notes(json.loads(args.notes.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            log.error("cannot use --notes %s: %s", args.notes, exc)
            return USAGE
    try:
        out = dashboard.write(args.run_dir, args.output, notes=notes)
    except dashboard.CredentialFound as exc:
        log.error("dashboard NOT written: possible credential in the run data (%s)", exc)
        return NOT_DELIVERED

    info = dashboard.summary(args.run_dir)
    counts = info["counts"]
    emit(
        f"dashboard: {out}  ({sum(counts.values())} tests: {counts.get('pass', 0)} pass, "
        f"{counts.get('fail', 0) + counts.get('error', 0)} fail, {counts.get('skip', 0)} skip"
        f"{', run not finished' if info['in_progress'] else ''})"
    )
    if info["clusters"]:
        emit("failures by first failing check:")
        for group in info["clusters"]:
            emit(f"  {group['count']:4d}  {group['key']}")
    if notes:
        unmatched = dashboard.unmatched_notes(args.run_dir, notes)
        attached = len(notes.get("cases") or {}) - sum(
            not k.startswith(("intent: ", "group: ")) for k in unmatched
        )
        intents = notes.get("intents") or {}
        emit(
            f"notes: {attached} case note(s) and "
            f"{len(intents) - sum(k.startswith('intent: ') for k in unmatched)} intent(s) attached"
        )
        for key in unmatched:
            log.warning("note key matches no test, so it is not shown: %s", key)
        if intents:
            missing = [case for case in info["case_ids"] if case not in intents]
            if missing:
                log.warning(
                    "%d case(s) have no intent: %s%s",
                    len(missing),
                    ", ".join(missing[:10]),
                    " ..." if len(missing) > 10 else "",
                )
    return OK
