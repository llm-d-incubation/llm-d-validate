"""`llmdv job` -- one unattended run: verify, then make sure it is published and notified.

`llmdv verify` runs as a child process, so that this one outlives it: if verify is OOM-killed,
the fallback publish and the notification still happen here. Arguments `llmdv job` does not
define (`--client`, `-k`, `--label`, ...) are passed through to verify unchanged.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..compare.archive import NIGHTLY_TRIGGER, publish_one
from ..evidence import alerts, dashboard, outcome
from ..log.setup import emit, get_logger
from ..output.remote import UnsupportedRemote, parse_remote_uri
from ..output.s3 import missing_credentials_reason
from .compare_cmd import compare_run
from .exit_codes import NOT_DELIVERED, OK, TEST_FAILURES, USAGE
from .notify_cmd import WEBHOOK_ENV, notify_run
from .publish_cmd import publish_run

log = get_logger("cli.job")

# Verify options `llmdv job` sets itself; passing them through as well would let verify and the
# fallback publish disagree.
OWNED = ("--profile", "--local-output", "--remote-output")


def add_parser(subparsers, parents) -> None:
    command = subparsers.add_parser(
        "job",
        parents=parents,
        allow_abbrev=False,
        help="one unattended run: verify, publish (even after a crash) and notify",
    )
    command.add_argument(
        "--profile",
        default=os.environ.get("LLMDV_PROFILE", "/etc/llmdv/profile.yaml"),
        help="profile to verify (env LLMDV_PROFILE; default /etc/llmdv/profile.yaml)",
    )
    command.add_argument(
        "--remote-output",
        default=os.environ.get("LLMDV_RESULTS_URI") or None,
        help="s3://bucket/prefix or file:///path (env LLMDV_RESULTS_URI); "
        "unset keeps results local; "
        "S3 is skipped when credentials for the selected authentication mode are unavailable",
    )
    command.add_argument(
        "--fallback-output",
        default=os.environ.get("LLMDV_FALLBACK_URI") or None,
        help="file:///path (e.g. a mounted PVC) used only when --remote-output is unavailable "
        "(env LLMDV_FALLBACK_URI)",
    )
    command.add_argument(
        "--work-dir",
        type=Path,
        default=Path(os.environ.get("LLMDV_WORK_DIR", "runs")),
        help="where run directories are written (env LLMDV_WORK_DIR; default ./runs)",
    )
    command.add_argument(
        "--budget",
        type=float,
        default=float(os.environ.get("LLMDV_RUN_BUDGET", "3000")),
        help="seconds before the run is stopped and published (env LLMDV_RUN_BUDGET; "
        "default 3000). Keep it below the Job's activeDeadlineSeconds",
    )
    _add_analyze_flag(command)
    _add_compare_flags(command)

    command.set_defaults(func=cmd_job)


def _publish_dashboard(run_dir: Path, destination: str | None) -> None:
    """Build dashboard.html, and send it after a run verify has already published. Best effort."""
    page = dashboard.write_best_effort(run_dir)
    if page is None:
        return
    log.info("dashboard: %s", page)
    body = outcome.read(run_dir) or {}
    if not destination or (body.get("publication") or {}).get("status") != "published":
        return
    profile = (body.get("profile") or {}).get("name")
    run_id = body.get("run_id")
    if not (profile and run_id):
        return
    try:
        log.info("published dashboard.html to %s", publish_one(destination, profile, run_id, page))
    except Exception as exc:  # noqa: BLE001 -- advisory; the run itself is already published
        log.warning("could not publish dashboard.html: %s", exc)


def _add_analyze_flag(command) -> None:
    command.add_argument(
        "--analyze",
        action="store_true",
        help="write dashboard.html into the run directory once alerts and the comparison exist, "
        "and publish it with the run (off by default)",
    )


def _add_compare_flags(command) -> None:
    command.add_argument(
        "--compare",
        action="store_true",
        help="after publishing, compare this run to the latest nightly run(s) of the same "
        "profile and include the analysis in the notification",
    )
    command.add_argument(
        "--compare-window",
        type=int,
        default=1,
        help="how many earlier runs must agree before a change is called a regression "
        "(default 1: the most recent run); a larger window reports a case they disagree "
        "about as flaky rather than paging on it",
    )
    command.add_argument(
        "--compare-baseline",
        default=None,
        help="run id to compare against instead of the nightly baseline, e.g. the run from "
        "before a rollout",
    )


def cmd_job(args: argparse.Namespace) -> int:
    passthrough = list(getattr(args, "pytest_args", None) or [])
    owned = [arg for arg in passthrough if _is_owned(arg)]
    if owned:
        log.error("%s is set by `llmdv job` itself, not passed through to verify", owned[0])
        return USAGE

    if args.fallback_output:
        try:
            parse_remote_uri(args.fallback_output)
        except UnsupportedRemote as exc:
            log.error("%s", exc)
            return USAGE

    requested_remote = args.remote_output
    publication_skipped = None
    if requested_remote:
        try:
            target = parse_remote_uri(requested_remote)
            if target.scheme == "s3":
                publication_skipped = missing_credentials_reason()
        except UnsupportedRemote as exc:
            log.error("%s", exc)
            return USAGE
        if publication_skipped:
            log.warning("S3 publication skipped: %s; results stay local", publication_skipped)
            # Both verification's preflight/upload and the fallback must use this decision.
            args.remote_output = None
    else:
        log.warning("no results destination configured; publication skipped, results stay local")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    run_dir = (args.work_dir / stamp).resolve()
    verify_code = _verify(args, run_dir, passthrough)

    # Compare BEFORE publishing, so comparison.md is part of the upload rather than a file
    # written into a pod's temporary directory after the upload has already gone. The
    # finishing run is read from run_dir, so the archive only has to supply the baseline --
    # which means a failed publication cannot make this compare two older runs instead.
    compare_code = OK
    if args.compare:
        if not requested_remote:
            log.error("--compare needs --remote-output; there is no archive to compare against")
            compare_code = USAGE
        elif run_dir.is_dir():
            started = outcome.read(run_dir) or {}
            profile_name = (started.get("profile") or {}).get("name")
            if profile_name:
                # Uses the configured destination, not args.remote_output, which is cleared
                # when credentials are missing. A skipped publication still has an archive
                # worth reading, and compare_run degrades quietly if it cannot reach it.
                compare_code, _ = compare_run(
                    run_dir,
                    requested_remote,
                    profile_name,
                    args.compare_baseline,
                    args.compare_window,
                )
            else:
                log.warning("the run recorded no profile name; nothing to compare")

    # The alert feed joins the verdicts with the comparison, so it is written after one. Its
    # reader takes it from the job log; a run verify did not publish carries it in the upload
    # below. It is not sent on its own: that would land it after the manifest.
    feed = _alert_feed(run_dir)

    # The dashboard renders alerts.json and comparison.json, which exist only from here on, so
    # the job builds it now rather than having the verify child build it before they were
    # written. If verify already published the run, the page follows on its own, as the
    # comparison does; otherwise the upload below carries it.
    analyze = getattr(args, "analyze", False)
    if analyze and run_dir.is_dir():
        _publish_dashboard(run_dir, args.remote_output)

    publish_code = OK
    unavailable = None
    if args.remote_output and run_dir.is_dir():
        publish_code = publish_run(run_dir, args.remote_output, verify_code)
        if publish_code != OK:
            error = ((outcome.read(run_dir) or {}).get("publication") or {}).get("error")
            unavailable = {"destination": args.remote_output, "error": error or "upload failed"}
    elif publication_skipped:
        unavailable = {"destination": requested_remote, "error": publication_skipped}

    tried_fallback = bool(unavailable and args.fallback_output and run_dir.is_dir())
    if tried_fallback:
        log.warning(
            "%s is unavailable; publishing to the fallback %s",
            unavailable["destination"],
            args.fallback_output,
        )
        publish_code = publish_run(
            run_dir,
            args.fallback_output,
            verify_code,
            primary=unavailable,
        )

    body = outcome.read(run_dir)
    if publication_skipped and body is not None and not tried_fallback:
        body["publication"] = {
            "status": "skipped",
            "destination": requested_remote,
            "reason": publication_skipped,
        }
        outcome.write_dict(run_dir, body)

    notify_code = OK
    if (os.environ.get(WEBHOOK_ENV) or "").strip():
        notify_code = notify_run(run_dir, verify_code)
    else:
        log.warning("$%s is not set or is empty; notification skipped", WEBHOOK_ENV)

    # In the job log too: if publication failed, this copy outlives the pod's emptyDir.
    if body is not None:
        emit((run_dir / outcome.FILENAME).read_text(encoding="utf-8").rstrip())

    code = _exit_code(verify_code, compare_code, publish_code, notify_code)
    # The feed's final form goes last in the log, where an alerting system that reads the
    # finished Job's log finds it without access to the results store.
    if feed is not None:
        _print_alert_feed(run_dir, feed, code)
    return code


# The alert feed is advisory, like the comparison. A failure to build, write or print it is
# logged and costs neither publication, notification nor the exit code.


def _alert_feed(run_dir: Path) -> dict | None:
    if not run_dir.is_dir():
        return None
    try:
        feed = alerts.build(run_dir)
    except Exception as exc:  # noqa: BLE001 -- advisory, never fatal
        log.warning("no alert feed: %s", exc)
        return None
    if feed is not None:
        _write_alert_feed(run_dir, feed)
    return feed


def _print_alert_feed(run_dir: Path, feed: dict, code: int) -> None:
    """Stamp the feed with how the job ended, then print it as the last line of the log."""
    try:
        feed["publication"] = ((outcome.read(run_dir) or {}).get("publication")) or {}
        feed["job_exit_code"] = code
        text = alerts.line(feed)
    except Exception as exc:  # noqa: BLE001 -- advisory, never fatal
        log.warning("no alert feed: %s", exc)
        return
    _write_alert_feed(run_dir, feed)
    emit(text)


def _write_alert_feed(run_dir: Path, feed: dict) -> None:
    try:
        alerts.write(run_dir, feed)
    except Exception as exc:  # noqa: BLE001 -- the job log still carries the feed
        log.warning("could not write %s: %s", alerts.FILENAME, exc)


def _exit_code(verify_code: int, compare_code: int, publish_code: int, notify_code: int) -> int:
    # A verification failure always wins. Verify's own NOT_DELIVERED (its upload failed) is
    # superseded by the fallback publish above, which always runs when results go anywhere.
    if verify_code not in (OK, NOT_DELIVERED):
        return verify_code
    # A run can pass every case and still have regressed against the previous one. That is the
    # question a nightly job exists to answer, so it decides the exit code when verification
    # itself was clean.
    if compare_code == TEST_FAILURES:
        return TEST_FAILURES
    if compare_code == USAGE:
        return USAGE
    return NOT_DELIVERED if NOT_DELIVERED in (publish_code, notify_code) else OK


def _verify(args: argparse.Namespace, run_dir: Path, passthrough: list[str]) -> int:
    """Run `llmdv verify` as a child within the budget; return its exit code, 128+N if killed."""
    command = [
        sys.executable, "-m", "llmdv.cli.main", "verify",
        "--profile", args.profile,
        "--local-output", str(run_dir),
        "--log-level", args.log_level,
        "--log-format", args.log_format,
        *(["--remote-output", args.remote_output] if args.remote_output else []),
        *(["--no-color"] if args.no_color else []),
        *(["-v"] * args.verbose),
        *(["--kubeconfig", str(args.kubeconfig)] if args.kubeconfig else []),
        *(["--context", args.context] if args.context else []),
        *(["--kube-cli", args.kube_cli] if getattr(args, "kube_cli", None) is not None else []),
        "--label", f"trigger={NIGHTLY_TRIGGER}",
        *passthrough,
    ]  # fmt: skip
    child = subprocess.Popen(command)  # noqa: S603 -- our own interpreter and CLI

    # SIGTERM -- the pod being stopped -- is passed on, so verify stops, records and publishes.
    def forward(signum, frame) -> None:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    try:
        code = child.wait(timeout=args.budget)
    except subprocess.TimeoutExpired:
        log.warning("the run budget of %.0fs is spent; stopping the run", args.budget)
        forward(signal.SIGTERM, None)
        code = child.wait()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    return 128 - code if code < 0 else code


def _is_owned(arg: str) -> bool:
    name = arg.split("=", 1)[0]
    # Prefixes too: verify's parser accepts abbreviations, so `--remote` would mean --remote-output.
    return len(name) > 2 and name.startswith("--") and any(o.startswith(name) for o in OWNED)
