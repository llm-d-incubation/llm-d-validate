"""The `llmdv` entry point.

argparse rather than Typer, for two reasons: Click and Typer write help and errors straight to
stdout/stderr, which fights the single-logger contract this tool is built around; and
`llmdv verify` forwards unknown arguments to pytest, which `parse_known_args` does plainly
while Click needs `ignore_unknown_options` plus `allow_extra_args` and would see a
pytest-bound `-p` or `-k` as its own.

The CLI layer stays deliberately thin -- parse, build a config, call a function -- so that
decision stays reversible without touching the library.

Global flags live on a parent parser passed to every subparser, which is what lets them be
given before or after the subcommand.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import __version__
from ..log.setup import configure, get_logger
from . import (
    analyze_cmd,
    compare_cmd,
    job_cmd,
    notify_cmd,
    profile_cmd,
    publish_cmd,
    style,
    verify_cmd,
)
from .exit_codes import INTERNAL, USAGE

log = get_logger("cli")


def _global_parser() -> argparse.ArgumentParser:
    """Flags shared by every subcommand. add_help=False so it can be a parent."""
    parent = argparse.ArgumentParser(add_help=False)
    logging_group = parent.add_argument_group("logging")
    logging_group.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="console verbosity for diagnostics (default: INFO). "
        "Primary output is never suppressed by this.",
    )
    logging_group.add_argument(
        "-v",
        dest="verbose",
        action="count",
        default=0,
        help="-v prints the recorded checks under each case; -vv also turns the console to DEBUG",
    )
    logging_group.add_argument(
        "-q", dest="quiet", action="store_true", help="only warnings and errors"
    )
    logging_group.add_argument(
        "--log-format",
        default="text",
        choices=["text", "json"],
        help="json emits one object per line, for pods and CI",
    )
    logging_group.add_argument("--log-file", default=None, help="write an additional log file here")
    logging_group.add_argument(
        "--no-color",
        action="store_true",
        help="never emit colour or box drawing, even on a terminal",
    )

    kube_group = parent.add_argument_group("cluster (read-only)")
    kube_group.add_argument(
        "--kube-cli",
        # A subcommand's default must not overwrite a value supplied before it, even "".
        default=argparse.SUPPRESS,
        help="cluster CLI name or path for read-only lookups (default: kubectl, then oc)",
    )
    kube_group.add_argument(
        "--kubeconfig",
        default=None,
        type=Path,
        help="kubeconfig for read-only lookups; overrides the profile",
    )
    kube_group.add_argument(
        "--context", default=None, help="kubeconfig context; overrides the profile"
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    parent = _global_parser()
    parser = argparse.ArgumentParser(
        prog="llmdv",
        parents=[parent],
        description="Functional validation for deployed llm-d model serving stacks. "
        "Tests already-deployed stacks; never deploys or mutates anything.",
        epilog="exit codes: 0 ok | 1 test failures (the fleet) | 2 usage or invalid profile | "
        "3 preflight unsatisfied (your vantage point) | 4 internal error | "
        "5 verification passed but results or notification not delivered | "
        "6 interrupted before completion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"llmdv {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    profile_cmd.add_parser(subparsers, [parent])
    verify_cmd.add_parser(subparsers, [parent])
    publish_cmd.add_parser(subparsers, [parent])
    job_cmd.add_parser(subparsers, [parent])
    notify_cmd.add_parser(subparsers, [parent])
    compare_cmd.add_parser(subparsers, [parent])
    analyze_cmd.add_parser(subparsers, [parent])
    return parser


def _resolve_level(args: argparse.Namespace) -> str:
    """Console level. `-v` deliberately does NOT raise it.

    `-v` asks to see the evidence behind each case, which is a llmdv concern. Raising the level to
    DEBUG would also unmute httpx and the SDKs and bury that evidence in transport chatter, so
    the two are separated: -v shows checks, -vv turns everything up.
    """
    if args.quiet:
        return "WARNING"
    if args.verbose >= 2:
        return "DEBUG"
    return args.log_level


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # parse_known_args rather than parse_args: `llmdv verify` forwards anything it does not
    # recognise to pytest, so `llmdv verify --profile p -x --tb=short` works. The cost is that a
    # typo in a llmdv flag reaches pytest instead of being caught here -- pytest then rejects it,
    # and _map_pytest_exit turns that into exit 2 rather than a fleet verdict.
    args, unknown = parser.parse_known_args(argv)
    args.pytest_args = unknown
    args.log_level = _resolve_level(args)
    style.force_plain(args.no_color)

    configure(
        level=args.log_level,
        fmt=args.log_format,
        log_file=Path(args.log_file) if args.log_file else None,
    )

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        log.warning("interrupted")
        return INTERNAL
    except argparse.ArgumentError as exc:
        log.error("%s", exc)
        return USAGE
    except Exception:  # noqa: BLE001 -- never surface a traceback as a fleet result
        log.exception("llmdv failed internally")
        return INTERNAL


if __name__ == "__main__":
    sys.exit(main())
