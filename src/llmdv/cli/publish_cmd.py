"""`llmdv publish` -- the fallback upload for a run `llmdv verify` could not publish itself.

A no-op for a run already published to the same destination. Otherwise, e.g. after an OOM kill,
it records the run as crashed, counts the results it did write, and uploads everything there is.

The exit code never lets a delivery problem replace a verification verdict: an upload that fails
returns the verification's own code when that is not a pass, and otherwise 3 for a store that
cannot be used at all (as `llmdv verify` says at preflight) or 5 for an upload that failed.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from ..evidence import dashboard
from ..evidence import outcome as outcome_file
from ..evidence.outcome import now, read_results, summarise
from ..http.redact import scrub
from ..log.setup import get_logger
from ..output.remote import (
    PublicationStatus,
    UnsupportedRemote,
    deliver,
    missing_credentials,
    parse_remote_uri,
    sink_for,
)
from .exit_codes import NOT_DELIVERED, OK, PREFLIGHT, USAGE, class_of

log = get_logger("cli.publish")

# How many unreadable result lines outcome.json names; the log names them all.
MAX_UNREADABLE = 20


def add_parser(subparsers, parents) -> None:
    command = subparsers.add_parser(
        "publish",
        parents=parents,
        help="upload a run directory that `llmdv verify` did not publish itself",
    )
    command.add_argument("run_dir", type=Path, help="the run directory `llmdv verify` wrote")
    command.add_argument(
        "--remote-output",
        required=True,
        help="s3://bucket/prefix or file:///path, as given to `llmdv verify`",
    )
    command.add_argument(
        "--exit-code",
        type=int,
        default=None,
        help="the exit code `llmdv verify` ended with; recorded when the run could not record it",
    )
    command.add_argument(
        "--analyze",
        action="store_true",
        help="write dashboard.html into the run directory before uploading it (off by default)",
    )
    command.set_defaults(func=cmd_publish)


def cmd_publish(args: argparse.Namespace) -> int:
    if getattr(args, "pytest_args", None):
        log.error("unrecognised arguments: %s", " ".join(args.pytest_args))
        return USAGE
    try:
        parse_remote_uri(args.remote_output)
    except UnsupportedRemote as exc:
        log.error("%s", exc)
        return USAGE
    code, unreadable = _publish_run(
        args.run_dir, args.remote_output, args.exit_code, None, analyze=args.analyze, check=True
    )
    if code == OK and unreadable:
        log.error(
            "the run was published, but %d line(s) of results.jsonl could not be read and are "
            "not in its counts",
            len(unreadable),
        )
        return USAGE
    return code


def publish_run(
    run_dir: Path,
    remote_output: str,
    exit_code: int | None,
    primary: dict | None = None,
    *,
    analyze: bool = False,
) -> int:
    """Upload a run unless it is already published there; OK only when it is delivered.

    Otherwise USAGE (nothing to publish, or the run is already published somewhere else), or
    why the upload did not happen: the verification's own code when it did not pass, since a
    delivery failure never replaces that; else PREFLIGHT when the destination needs credentials
    that are not configured, so nothing was sent; else NOT_DELIVERED.

    `primary` records the destination that was unavailable when this is a fallback upload.
    """
    return _publish_run(run_dir, remote_output, exit_code, primary, analyze=analyze)[0]


def _publish_run(
    run_dir: Path,
    remote_output: str,
    exit_code: int | None,
    primary: dict | None,
    *,
    analyze: bool,
    check: bool = False,
) -> tuple[int, list[str]]:
    """`publish_run`, plus the result lines that could not be read. `check` tries the store
    before uploading, as preflight does, so one that cannot be used at all is PREFLIGHT."""
    if not run_dir.is_dir() or not any(run_dir.iterdir()):
        log.error("%s is empty or missing, so there is nothing to publish", run_dir)
        return USAGE, []
    unreadable: list[str] = []
    code = _publish(run_dir, remote_output, exit_code, primary, unreadable, analyze, check)
    return code, unreadable


def record_verdict(run_dir: Path, body: dict, exit_code: int | None) -> list[str] | None:
    """Fill in the verdict of a run that died before recording one, from the exit code its
    process ended with and the results it left. None when the run recorded its own; otherwise
    the result lines that could not be read, which are named in the outcome and not counted."""
    verification = body.setdefault("verification", {})
    if verification.get("exit_code") is not None:
        return None
    unreadable: list[str] = []
    counts, skips, failures = summarise(read_results(run_dir, unreadable))
    verification.update(
        exit_code=exit_code, **{"class": class_of(exit_code)}, counts=counts, skips=skips,
        failures=failures,
    )  # fmt: skip
    if unreadable:
        verification["unreadable_results"] = unreadable[:MAX_UNREADABLE]
    body["finished"] = now()
    log.warning("the run ended (exit %s) before recording its outcome", exit_code)
    for line in unreadable:
        log.error("not counted: %s", line)
    return unreadable


def _same_destination(one: str, other: str) -> bool:
    return one.rstrip("/") == other.rstrip("/")


def _publish(
    run_dir: Path,
    remote_output: str,
    exit_code: int | None,
    primary: dict | None,
    unreadable: list[str],
    analyze: bool,
    check: bool,
) -> int:
    body = outcome_file.read(run_dir)
    if body is None:
        # Missing, cut short or malformed: the run died before, or while, recording it, or the
        # file is not an outcome at all. Publish anyway.
        reason = outcome_file.malformed(run_dir) or f"{run_dir} has no readable outcome.json"
        log.warning("%s; rebuilding what can be known", reason)
        body = _rebuilt_outcome(run_dir)

    previous = body.get("publication") or {}
    if previous.get("status") == PublicationStatus.PUBLISHED:
        there = previous.get("destination")
        if there is None or _same_destination(there, remote_output):
            log.info("already published to %s", previous.get("location"))
            return OK
        log.error(
            "%s is already published to %s (%s); a run is published to one destination, so "
            "nothing was sent to %s. Copy the published run from there instead",
            run_dir,
            there,
            previous.get("location"),
            remote_output,
        )
        return USAGE

    unreadable += record_verdict(run_dir, body, exit_code) or []
    verified = body["verification"].get("exit_code")
    fallback = {"primary": primary} if primary else {}
    body["publication"] = {"destination": remote_output, **fallback}
    outcome_file.write_dict(run_dir, body)

    if analyze:
        # Rebuilt from the outcome just written, so a crashed run's dashboard says so. Best
        # effort: it never stops the upload.
        dashboard.write_best_effort(run_dir)

    reason = _missing_credentials(remote_output) or (check and _unusable(remote_output))
    if reason:
        body["publication"] = {
            "destination": remote_output,
            "status": PublicationStatus.FAILED,
            "error": reason,
            **fallback,
        }
        outcome_file.write_dict(run_dir, body)
        log.error("results were NOT published: %s", reason)
        return _not_delivered(verified, PREFLIGHT)

    name = f"{(body.get('profile') or {}).get('name') or 'unknown'}/{body['run_id']}"
    body["publication"] = {**deliver(run_dir, remote_output, name), **fallback}
    outcome_file.write_dict(run_dir, body)
    if body["publication"]["status"] != PublicationStatus.PUBLISHED:
        log.error("results were NOT published: %s", body["publication"]["error"])
        return _not_delivered(verified, NOT_DELIVERED)
    log.info("published to %s", body["publication"]["location"])
    return OK


def _not_delivered(verified: int | None, code: int) -> int:
    """The exit code for a run that was not published. A verification that did not pass keeps
    its own code; `code` is only for one that passed, or whose code nobody recorded."""
    return code if verified in (None, OK, NOT_DELIVERED) else verified


def _missing_credentials(remote_output: str) -> str | None:
    try:
        return missing_credentials(remote_output)
    except UnsupportedRemote:
        # A missing SDK or a bad auth mode: the upload attempt reports it as not delivered.
        return None


def _unusable(remote_output: str) -> str | None:
    """Why nothing can be written to the destination, or None: the write check preflight makes."""
    try:
        sink_for(remote_output).check()
    except UnsupportedRemote:
        return None  # as above: the upload attempt reports it
    except Exception as exc:  # noqa: BLE001 -- whatever the store raises, it cannot be used
        return scrub(f"{remote_output} cannot be written to: {type(exc).__name__}: {exc}")
    return None


def _rebuilt_outcome(run_dir: Path) -> dict:
    """The least outcome a run directory supports: its run id and profile, from its own files."""
    records = read_results(run_dir, [])
    profile_name = "unknown"
    try:
        profile_name = yaml.safe_load((run_dir / "profile.used.yaml").read_text())["name"]
    except Exception:  # noqa: BLE001 -- best effort; the run is published under "unknown"
        pass
    return {
        "schema": outcome_file.SCHEMA,
        "run_id": (records[0].get("run_id") if records else None) or run_dir.name,
        "profile": {"name": str(profile_name)},
        "verification": {"exit_code": None},
    }
