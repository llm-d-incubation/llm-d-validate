"""`llmdv notify` -- post a run's outcome.json to Slack; `--exit-code` covers a run without one."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .. import notify
from ..compare.report import COMPARISON_FILE
from ..evidence import outcome
from ..http import redact
from ..log.setup import get_logger
from .exit_codes import NOT_DELIVERED, OK, USAGE

log = get_logger("cli.notify")

WEBHOOK_ENV = "LLMDV_NOTIFY_WEBHOOK"


def add_parser(subparsers, parents) -> None:
    command = subparsers.add_parser(
        "notify",
        parents=parents,
        help="send a finished run's outcome to Slack",
    )
    command.add_argument("run_dir", type=Path, help="the run directory `llmdv verify` wrote")
    command.add_argument(
        "--exit-code",
        type=int,
        default=None,
        help="the exit code `llmdv verify` returned; reported when the run left no outcome.json",
    )
    command.set_defaults(func=cmd_notify)


def cmd_notify(args: argparse.Namespace) -> int:
    if getattr(args, "pytest_args", None):
        log.error("unrecognised arguments: %s", " ".join(args.pytest_args))
        return USAGE
    return notify_run(args.run_dir, args.exit_code)


def notify_run(run_dir: Path, exit_code: int | None) -> int:
    """Post a run's outcome to the webhook in $LLMDV_NOTIFY_WEBHOOK; OK, NOT_DELIVERED or USAGE."""
    url = os.environ.get(WEBHOOK_ENV)
    if not url:
        log.error("$%s is not set, so there is nowhere to send the notification", WEBHOOK_ENV)
        return USAGE
    # The URL embeds the webhook's credential; register it so no log line can carry it.
    redact.install_secrets([url])

    # The job writes either the comparison or its unavailability reason beside the results.
    # Absent when no comparison was requested.
    comparison = None
    comparison_file = run_dir / COMPARISON_FILE
    if comparison_file.is_file():
        comparison = comparison_file.read_text(encoding="utf-8")

    text = notify.message(outcome.read(run_dir), exit_code, comparison)
    try:
        notify.post(url, text)
    except notify.NotifyError as exc:
        log.error("notification not sent: %s", exc)
        return NOT_DELIVERED
    log.info("notification sent")
    return OK
