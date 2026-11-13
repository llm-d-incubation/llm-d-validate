"""`llmdv publish` -- the fallback upload for a run `llmdv verify` could not publish itself.

A no-op for a published run. Otherwise, e.g. after an OOM kill, it records the run as crashed,
counts the results it did write, and uploads everything there is.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from ..evidence import dashboard
from ..evidence import outcome as outcome_file
from ..evidence.outcome import now, read_results, summarise
from ..log.setup import get_logger
from ..output.remote import UnsupportedRemote, deliver, parse_remote_uri
from .exit_codes import NOT_DELIVERED, OK, USAGE, class_of

log = get_logger("cli.publish")


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
    return publish_run(args.run_dir, args.remote_output, args.exit_code, analyze=args.analyze)


def publish_run(
    run_dir: Path,
    remote_output: str,
    exit_code: int | None,
    primary: dict | None = None,
    *,
    analyze: bool = False,
) -> int:
    """Upload a run unless it is already published; OK, NOT_DELIVERED or USAGE.

    `primary` records the destination that was unavailable when this is a fallback upload.
    """
    if not run_dir.is_dir() or not any(run_dir.iterdir()):
        log.error("%s is empty or missing, so there is nothing to publish", run_dir)
        return USAGE
    body = outcome_file.read(run_dir)
    if body is None:
        # Missing or unreadable: the run died before, or while, recording it. Publish anyway.
        log.warning("%s has no readable outcome.json; rebuilding what can be known", run_dir)
        body = _rebuilt_outcome(run_dir)

    if (body.get("publication") or {}).get("status") == "published":
        log.info("already published to %s", body["publication"].get("location"))
        return OK

    verification = body.setdefault("verification", {})
    if verification.get("exit_code") is None:
        # The run died before recording its verdict. Say so, and keep what it did record.
        code = exit_code
        counts, skips, failures = summarise(read_results(run_dir))
        verification.update(
            exit_code=code, **{"class": class_of(code)}, counts=counts, skips=skips,
            failures=failures,
        )  # fmt: skip
        body["finished"] = now()
        log.warning(
            "the run ended (exit %s) before recording its outcome; publishing what it left",
            code,
        )
    fallback = {"primary": primary} if primary else {}
    body["publication"] = {"destination": remote_output, **fallback}
    outcome_file.write_dict(run_dir, body)

    if analyze:
        # Rebuilt from the outcome just written, so a crashed run's dashboard says so. Best
        # effort: it never stops the upload.
        dashboard.write_best_effort(run_dir)

    name = f"{(body.get('profile') or {}).get('name') or 'unknown'}/{body['run_id']}"
    body["publication"] = {**deliver(run_dir, remote_output, name), **fallback}
    outcome_file.write_dict(run_dir, body)
    if body["publication"]["status"] != "published":
        log.error("results were NOT published: %s", body["publication"]["error"])
        return NOT_DELIVERED
    log.info("published to %s", body["publication"]["location"])
    return OK


def _rebuilt_outcome(run_dir: Path) -> dict:
    """The least outcome a run directory supports: its run id and profile, from its own files."""
    records = read_results(run_dir)
    profile_name = "unknown"
    try:
        profile_name = yaml.safe_load((run_dir / "profile.used.yaml").read_text())["name"]
    except Exception:  # noqa: BLE001 -- best effort; the run is published under "unknown"
        pass
    return {
        "schema": outcome_file.SCHEMA,
        "run_id": records[0]["run_id"] if records else run_dir.name,
        "profile": {"name": str(profile_name)},
        "verification": {"exit_code": None},
    }
