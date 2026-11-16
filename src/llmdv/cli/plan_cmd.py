"""`llmdv plan` -- list what `llmdv verify` would run and skip, without running anything.

The plan is not a second reading of the skip rules. It is pytest's own collection, with the
llmdv plugin and the selectors `verify` passes, stopped at `--collect-only`. Each collected case
is then asked the setup questions a run asks first -- pytest's skip marks, then the plugin's
gate -- through the same functions. Nothing past that is guessed: a case whose fixtures or body
decide to skip (a 429 under `rate_limited`, a disposition only an instrument can see) is planned
to run, and the run decides.

No request is sent, no connection opened, no preflight run and no cluster read.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from ..log.setup import emit, get_logger
from ..profile import loader
from .exit_codes import OK, USAGE
from .verify_cmd import (
    SUITES_DIR,
    _map_pytest_exit,
    _plugin_is_registered,
    _run_pytest,
    _unknown_selectors,
    add_selection_arguments,
    selection_argv,
)

log = get_logger("cli.plan")

SCHEMA = "llmdv.plan/1"

DURING_THE_RUN = (
    "a case that would run can still skip from inside, decided during the run: "
    "throttled, requires_instrumentation, or a declaration the case itself reads"
)


def add_parser(subparsers, parents) -> None:
    plan = subparsers.add_parser(
        "plan",
        parents=parents,
        help="list the cases verify would run and skip, without sending a request",
        description="List every case `llmdv verify` would run with the same selectors, and "
        "every case it would skip with its skip class and reason. Offline: no request, no "
        "preflight, no cluster.",
    )
    add_selection_arguments(plan)
    plan.add_argument(
        "--json", action="store_true", help=f"print a {SCHEMA} document instead of text"
    )
    plan.set_defaults(func=cmd_plan)


class _Planner:
    """Records each collected case's planned outcome once collection has settled."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def pytest_collection_finish(self, session) -> None:
        from ..plugin import plan_row

        self.rows = [plan_row(item) for item in session.items]


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        profile = loader.load(args.profile)
    except loader.ProfileError as exc:
        log.error("%s", exc)
        return USAGE
    errors = [item for item in loader.validate(profile) if item.level == "error"]
    for item in errors:
        log.error("profile: %s", item)
    if errors:
        log.error("refusing to plan against an invalid profile (%d error(s))", len(errors))
        return USAGE
    bad = _unknown_selectors(profile, args)
    for message in bad:
        log.error("%s", message)
    if bad:
        return USAGE

    argv = [str(SUITES_DIR), "--llmdv-profile", args.profile, "--collect-only", "-q"]
    argv += ["--no-header", "-p", "no:xdist", "-p", "no:cacheprovider"]
    if not _plugin_is_registered():
        argv[1:1] = ["-p", "llmdv.plugin"]
    argv += selection_argv(args)
    log.debug("pytest argv: %s", " ".join(argv))

    planner = _Planner()
    code = _map_pytest_exit(_run_pytest(argv, raw=False, setup_commands={}, plugins=(planner,)))
    if code != OK:
        return code

    document = _document(profile.name, args, planner.rows)
    if args.json:
        emit(json.dumps(document, indent=2))
    else:
        for line in _text(document, counts_only=args.quiet):
            emit(line)
    return OK


def _document(name: str, args: argparse.Namespace, rows: list[dict]) -> dict:
    by_suite: dict[str, dict[str, int]] = {}
    for row in rows:
        tally = by_suite.setdefault(row["suite"] or "other", {"run": 0, "skip": 0})
        tally[row["outcome"]] += 1
    by_class = Counter(
        row["skip_class"] or "unclassified" for row in rows if row["outcome"] == "skip"
    )
    return {
        "schema": SCHEMA,
        "profile": {"name": name, "path": str(Path(args.profile).resolve())},
        "selection": {
            "ingress": args.ingress,
            "client": args.client,
            "surface": args.surface,
            "keyword": args.keyword,
            "marker": args.marker,
            "pytest_args": [item for item in (args.pytest_args or []) if item != "--"],
        },
        "counts": {
            "total": len(rows),
            "run": sum(1 for row in rows if row["outcome"] == "run"),
            "skip": sum(1 for row in rows if row["outcome"] == "skip"),
            "by_suite": dict(sorted(by_suite.items())),
            "by_skip_class": dict(sorted(by_class.items())),
        },
        "note": DURING_THE_RUN,
        "cases": rows,
    }


def _text(document: dict, counts_only: bool) -> list[str]:
    counts = document["counts"]
    lines = [
        f"{document['profile']['name']}: {counts['total']} cases, "
        f"{counts['run']} would run, {counts['skip']} would skip before running",
        "",
    ]
    width = max([len("suite"), *map(len, counts["by_suite"])])
    lines.append(f"  {'suite'.ljust(width)}   run  skip")
    for suite, tally in counts["by_suite"].items():
        lines.append(f"  {suite.ljust(width)} {tally['run']:5} {tally['skip']:5}")
    if counts["by_skip_class"]:
        lines += ["", "  skipped by class:"]
        for skip_class, count in counts["by_skip_class"].items():
            lines.append(f"    {count:5}  {skip_class}")
    lines += ["", f"Note: {DURING_THE_RUN}."]
    if counts_only:
        return lines

    # Grouped by suite in run order. A suite's phases can be apart in the run (fleet's `logs`
    # runs last), so grouping by consecutive rows would print a heading twice.
    groups: dict[str, list[dict]] = {}
    for row in document["cases"]:
        groups.setdefault(row["suite"] or "other", []).append(row)
    for suite, rows in groups.items():
        lines += ["", f"## {suite}"]
        for row in rows:
            case = row["case_id"] or row["nodeid"].split("::")[-1]
            if row["variant"]:
                case = f"{case} [{row['variant']}]"
            line = f"  {row['outcome']:4}  {(row['ingress'] or '-'):10} {case}"
            if row["outcome"] == "skip":
                why = _strip_class(row["reason"])
                line += f"  -- {row['skip_class'] or 'unclassified'}: {why}"
            lines.append(line)
    return lines


def _strip_class(reason: str | None) -> str:
    """The reason without its leading `[class]`, which the line already shows."""
    text = reason or ""
    return text.split("] ", 1)[1] if text.startswith("[") and "] " in text else text
