"""`llmdv verify` -- run the functional suites against a live fleet.

Named `verify` rather than `run` deliberately. In these repositories "run" already means a
benchmark campaign (aiperf sweeps, briefing replays, the campaign reports), and this command
is the opposite of that: it generates no load and measures no performance, it checks whether
declared contracts hold. `run` and `execute` stay as undocumented aliases for muscle memory.

pytest is embedded rather than shelled out to, and its output is redirected onto a logger
instead of the terminal, so one `--log-level` governs everything the process emits. Disabling
pytest's terminal plugin outright would be the obvious alternative, but `-p no:terminal`
breaks third-party plugins that look up `terminalreporter`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__, preflight
from ..collect import KubectlLogCollector, NoopCollector
from ..evidence import dashboard, reproduce
from ..evidence.outcome import Outcome, known_summary, now
from ..evidence.stack import stack_from_run
from ..kube import KubeContext
from ..log import setup as log_setup
from ..log.setup import emit, get_logger
from ..log.writer import LoggerWriter
from ..output import (
    LocalDirSink,
    Offloader,
    UnsupportedRemote,
    default_run_dir,
    run_prefix,
)
from ..output.remote import deliver, parse_remote_uri
from ..profile import loader
from ..profile.schema import Profile
from ..results.record import new_run_id
from . import banner
from .exit_codes import (
    CLASS,
    INTERNAL,
    INTERRUPTED,
    NOT_DELIVERED,
    OK,
    PREFLIGHT,
    TEST_FAILURES,
    USAGE,
)

log = get_logger("cli.verify")

SUITES_DIR = Path(__file__).resolve().parent.parent / "suites"

_LABEL_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def add_parser(subparsers, parents) -> None:
    verify = subparsers.add_parser(
        "verify",
        parents=parents,
        aliases=["run", "execute"],
        help="run functional tests against a deployed fleet",
    )
    # No short form: -p is pytest's plugin flag, and trailing args are forwarded to pytest.
    verify.add_argument("--profile", required=True, help="path to an infra-profile YAML")
    verify.add_argument(
        "--ingress",
        action="append",
        default=[],
        help="restrict to this ingress id (repeatable; a union)",
    )
    verify.add_argument(
        "--client",
        action="append",
        default=[],
        help="restrict to surfaces this client needs (repeatable; a union)",
    )
    verify.add_argument(
        "--surface",
        action="append",
        default=[],
        help="restrict to this surface (repeatable; a union)",
    )
    verify.add_argument("-k", dest="keyword", default=None, help="pytest keyword expression")
    verify.add_argument("-m", dest="marker", default=None, help="pytest marker expression")
    verify.add_argument(
        "--local-output",
        default=None,
        help="run directory for results and evidence (default: ./runs/<utc>)",
    )
    verify.add_argument(
        "--remote-output",
        default=None,
        help="publish the finished run to s3://bucket/prefix or file:///path "
        "(checked writable at preflight)",
    )
    verify.add_argument(
        "--artifact-sink",
        default=None,
        help="store artifacts too large for the run directory whole in file:// or s3://; the run "
        "directory keeps the capped copy and a reference; checked writable at preflight",
    )
    verify.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="record a label in env.json and outcome.json, e.g. trigger=nightly or "
        "rollout=<id> (repeatable; a later key overrides an earlier one)",
    )
    verify.add_argument(
        "--transcript-cap",
        type=int,
        default=None,
        metavar="BYTES",
        help="cap a stored SSE transcript, dropping from the middle so the "
        "opening frames and the terminal events are both kept. "
        "0 keeps everything (default: 256 KiB)",
    )
    verify.add_argument(
        "--pod-logs",
        default="failures",
        choices=["off", "failures", "all"],
        help="collect pod logs for the run window: never, only when something "
        "failed (default), or always. Read-only; needs a kubeconfig",
    )
    verify.add_argument(
        "--evidence",
        default="failures",
        choices=["failures", "all"],
        help="write an evidence bundle for failures only (default), or for "
        "every case so a pass can be audited too",
    )
    verify.add_argument(
        "--analyze",
        action="store_true",
        help="when the run finishes, write dashboard.html into the run directory: every test "
        "with its evidence, rerun command and debugging hints. Built before publishing, so "
        "it is uploaded with the run. Off by default; `llmdv analyze <run_dir>` builds one later",
    )
    verify.add_argument(
        "--skip-preflight",
        action="store_true",
        help="do not check reachability first (a fleet failure may then be a setup problem)",
    )
    verify.add_argument(
        "--raw-pytest",
        action="store_true",
        help="let pytest write to the terminal directly instead of through the logger",
    )
    # Unrecognised arguments are forwarded to pytest by parse_known_args in cli/main.py.
    # argparse.REMAINDER is NOT used here: it greedily swallows later flags, which silently
    # routed llmdv's own --local-output into the pytest argv and made a bad --client look like a
    # pytest usage error instead of a llmdv one.
    verify.set_defaults(func=cmd_verify)


@dataclass
class _Run:
    """What one verification has established so far, for whichever way it ends."""

    run_dir: Path
    outcome: Outcome
    started: float = field(default_factory=time.monotonic)
    profile: Profile | None = None
    kube: KubeContext | None = None
    setup_commands: dict[str, list[str]] = field(default_factory=dict)
    ran_pytest: bool = False
    records: list[dict] = field(default_factory=list)
    table: list[str] = field(default_factory=list)
    collected: object | None = None
    preflight_report: preflight.Report = field(default_factory=preflight.Report)


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify, then record and publish the run however it ended.

    The run directory is created before the profile is read, so every ending leaves a bundle.
    """
    # Resolved to an absolute path before anything is written. The banner and the summary both
    # print it, and a relative "runs/<ts>" is ambiguous the moment it is pasted into a ticket or
    # read from a different working directory. Evidence references inside results.jsonl stay
    # relative to this directory, so a copied bundle is still self-contained.
    run_dir = (Path(args.local_output) if args.local_output else default_run_dir()).resolve()
    LocalDirSink(run_dir).prepare()

    # Re-point logging now that a run directory exists, so llmdv.log lands inside the bundle.
    log_setup.configure(
        level=args.log_level,
        fmt=args.log_format,
        log_file=Path(args.log_file) if args.log_file else None,
        run_log=run_dir / "llmdv.log",
    )

    run = _Run(
        run_dir=run_dir,
        outcome=Outcome(
            run_id=new_run_id(),
            profile={"name": Path(args.profile).stem, "path": str(Path(args.profile).resolve())},
        ),
    )
    # Written now and completed in _finish, so a killed run still leaves its run id and profile.
    run.outcome.write(run_dir)

    # SIGTERM (a stopped pod, the run budget) is handled like Ctrl-C: stop, then record and publish.
    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGINT, _interrupt)
    try:
        code = _verify(args, run)
    except KeyboardInterrupt:
        log.warning("interrupted before the run completed; what was recorded is kept")
        code = INTERRUPTED
    except Exception:  # noqa: BLE001 -- a llmdv defect must not look like a fleet failure
        log.exception("llmdv failed internally")
        code = INTERNAL
    return _finish(args, run, code)


def _verify(args: argparse.Namespace, run: _Run) -> int:
    run_dir = run.run_dir

    # Copied even when invalid: a refusal only means something against what was refused.
    if Path(args.profile).is_file():
        shutil.copyfile(args.profile, run_dir / "profile.used.yaml")

    parallel = _parallel_args(getattr(args, "pytest_args", None) or [])
    if parallel:
        log.error(
            "refusing %s: live verification runs one case at a time by design (cache reuse, "
            "sessions and rate limits depend on order); parallel workers are for llmdv's own "
            "offline tests only",
            " ".join(parallel),
        )
        return USAGE

    labels, bad_labels = _parse_labels(args.label)
    run.outcome.labels = labels
    for message in bad_labels:
        log.error("%s", message)
    if bad_labels:
        return USAGE

    try:
        profile = loader.load(args.profile)
    except loader.ProfileError as exc:
        log.error("%s", exc)
        return USAGE
    run.outcome.profile["name"] = profile.name
    run.outcome.write(run_dir)

    diagnostics = loader.validate(profile)
    errors = [item for item in diagnostics if item.level == "error"]
    for item in errors:
        log.error("profile: %s", item)
    if errors:
        log.error("refusing to run against an invalid profile (%d error(s))", len(errors))
        return USAGE

    # Warnings go to the log in full, but the banner gets one aggregated note per kind. The
    # not-implemented warning fires once per (ingress, surface) pair, which was six identical
    # lines on a two-ingress profile and buried everything else.
    notes: list[str] = []
    for item in diagnostics:
        log.debug("profile: %s", item)
    other = [
        item
        for item in diagnostics
        if item.level == "warning" and "not implemented" not in item.message
    ]
    for item in other:
        notes.append(f"{item.path}: {item.message}")

    # Selector names are checked here, before preflight dereferences them. A typo is a usage
    # error with a list of the valid names, not a traceback -- exit 2, not exit 4.
    bad = _unknown_selectors(profile, args)
    if bad:
        for message in bad:
            log.error("%s", message)
        return USAGE
    run.profile = profile

    kube = KubeContext(
        kubeconfig=args.kubeconfig or profile.environment.kubeconfig,
        context=args.context or profile.environment.context,
        namespace=profile.environment.namespace,
        cli=getattr(args, "kube_cli", None),
    )
    run.kube = kube

    selected = args.ingress or list(profile.ingress_ids)
    banner.start(profile, args.profile, selected, args, run_dir, notes, kube=kube)
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not args.skip_preflight:
        report = preflight.run(
            profile,
            selected,
            kube=kube,
            remote_output=args.remote_output,
            artifact_sink=getattr(args, "artifact_sink", None),
            require_logs=args.pod_logs == "all",
        )
        run.setup_commands = report.resolved_setup
        run.preflight_report = report
        for warning in report.warnings:
            log.warning("preflight: %s", warning.render())
        if not report.ok:
            run.outcome.preflight_problems = [problem.render() for problem in report.problems]
            log.error("preflight failed; not running any tests")
            for problem in report.problems:
                for line in problem.render().splitlines():
                    log.error("  %s", line)
            emit("")
            emit("preflight failed -- this is an environment problem, not a fleet result.")
            emit("Nothing was tested. Fix the above, or re-run with --skip-preflight.")
            return PREFLIGHT
        banner.phase("preflight", len(selected), unit="ingress")
        for ingress_id in selected:
            ingress = profile.ingress(ingress_id)
            banner.phase_line(
                "ok",
                ingress_id,
                ingress.base_url,
                f"{report.latency_ms.get(ingress_id, 0):.0f}ms",
            )
    elif args.remote_output or getattr(args, "artifact_sink", None):
        for uri, flag in (
            (args.remote_output, "--remote-output"),
            (getattr(args, "artifact_sink", None), "--artifact-sink"),
        ):
            if not uri:
                continue
            try:
                parse_remote_uri(uri, flag=flag)
            except UnsupportedRemote as exc:
                log.error("%s", exc)
                return USAGE

    pytest_argv = _build_pytest_argv(args, run_dir, run.outcome.run_id, started_at)
    log.debug("pytest argv: %s", " ".join(pytest_argv))

    run.ran_pytest = True
    code = _run_pytest(pytest_argv, raw=args.raw_pytest, setup_commands=run.setup_commands)

    records = _read_records(run_dir)
    run.collected = _collect_logs(
        args,
        profile,
        selected,
        kube,
        run_dir,
        started_at,
        records,
        run.preflight_report,
        run.outcome.run_id,
    )
    run.records, run.table = _summarise(run_dir, records, run.collected, run.profile)
    return _map_pytest_exit(code, _SIGNALLED.is_set())


def _finish(args: argparse.Namespace, run: _Run, code: int) -> int:
    """Record, freeze, publish; then decide the exit code by precedence.

    Precedence: the verification code if it is non-zero, so a test failure is never masked by
    a delivery failure; otherwise NOT_DELIVERED if publication failed; otherwise OK.
    """
    # A second signal must not cut publication short.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    run_dir, outcome = run.run_dir, run.outcome
    if run.profile is not None:
        # Supporting files only. Failing to write one must never stop the run being published.
        try:
            _write_run_metadata(
                run_dir, run.profile, args, run.setup_commands, outcome, run.collected, run.kube
            )
            _write_reproduce(args, run.profile, run_dir, run.setup_commands)
        except Exception:  # noqa: BLE001
            log.exception("could not write env.json or reproduce.sh; publishing without them")

    outcome.exit_code = code
    outcome.exit_class = CLASS[code]
    outcome.finished = now()
    outcome.record_results(run.records, run.profile.known_failures if run.profile else ())
    try:
        outcome.stack = stack_from_run(run_dir)
    except Exception:  # noqa: BLE001 -- a reading aid; it must never cost the outcome
        log.exception("could not summarise the stack from exchanges.jsonl")
    if args.remote_output:
        outcome.publication = {"destination": args.remote_output}
    outcome.write(run_dir)

    if getattr(args, "analyze", False):
        # A reading aid over evidence already written. It can never change the exit code or
        # stop the run being published: write_best_effort logs and returns None on any failure.
        path = dashboard.write_best_effort(run_dir)
        if path is not None:
            log.info("dashboard: %s", path)

    # Freeze the bundle: left open, llmdv.log keeps growing and stops matching the manifest.
    log_setup.close_run_log()

    published = None
    publication_failed = False
    if args.remote_output:
        name = f"{outcome.profile['name']}/{outcome.run_id}"
        outcome.publication = deliver(run_dir, args.remote_output, name)
        published = outcome.publication.get("location")
        if outcome.publication["status"] != "published":
            publication_failed = True
            log.error(
                "results were NOT published to %s: %s",
                args.remote_output,
                outcome.publication["error"],
            )
            log.error("the only copy is the local run directory: %s", run_dir)
    else:
        outcome.publication = {"status": "not_requested"}
    outcome.write(run_dir)

    if run.ran_pytest:
        banner.finish(
            run_dir,
            run.records,
            run.table,
            time.monotonic() - run.started,
            published,
            run.collected,
            layer_order(run.records, run.profile),
            known_unmatched=outcome.known.get("unmatched"),
        )
    if code != OK:
        return code
    return NOT_DELIVERED if publication_failed else OK


# Set only by a real signal. pytest reports collection errors as "interrupted" too, since its
# Session.Interrupted subclasses KeyboardInterrupt.
_SIGNALLED = threading.Event()


def _interrupt(signum, frame) -> None:
    _SIGNALLED.set()
    raise KeyboardInterrupt


def _parse_labels(values: list[str]) -> tuple[dict[str, str], list[str]]:
    """`key=value` pairs; a later key overrides an earlier one, e.g. a runner's default."""
    labels: dict[str, str] = {}
    problems: list[str] = []
    for value in values or []:
        key, sep, val = value.partition("=")
        if not sep or not _LABEL_KEY.fullmatch(key):
            problems.append(f"--label {value!r} must be key=value, with a key of [A-Za-z0-9_.-]")
            continue
        labels[key] = val
    return labels, problems


def _collect_logs(
    args, profile, selected, kube, run_dir, since_time, records, report, run_id="unknown"
):
    """Pull each layer's own account of the run, once, for the whole window.

    Collected per run rather than per failure: five failures against one decode pod would
    otherwise fetch five near-identical copies of the same log.

    Announces nothing it does not then do. The old version logged "collecting pod logs" before
    knowing whether anything was collectable, created the directory regardless, and left an
    empty one behind -- which reads, to anyone opening the bundle later, exactly like a run
    whose layers had nothing to say.
    """
    mode = args.pod_logs
    failed = any(r["outcome"] in ("fail", "error") for r in records)
    if mode == "off":
        reason = "not collected (--pod-logs off)"
    elif mode == "failures" and not failed:
        reason = "not collected (nothing failed, and --pod-logs is 'failures')"
    elif report.log_access in (preflight.LogAccess.AVAILABLE, preflight.LogAccess.UNKNOWN):
        # UNKNOWN attempts anyway: it means the probe was inconclusive or preflight was
        # skipped, and refusing to try would lose logs that were there for the taking. A real
        # failure then surfaces per pod, with the kubectl error attached.
        reason = None
    else:
        reason = report.log_access_reason

    if reason is not None:
        log.info("no pod logs: %s", reason)
        return NoopCollector(reason).collect(profile, selected, since_time, run_dir)

    log.info("collecting pod logs since %s", since_time)
    offloader = Offloader(
        getattr(args, "artifact_sink", None),
        run_prefix(profile.name, run_id),
    )
    collector = KubectlLogCollector(kube, profile.environment.namespace, offloader)
    collection = collector.collect(profile, selected, since_time, run_dir / "logs")
    for entry in collection.failed:
        log.warning("could not read logs for %s/%s: %s", entry.pod, entry.container, entry.error)
    if collection.reason:
        log.info("no pod logs: %s", collection.reason)
    if collection.logs:
        # Written whenever collection was attempted, not only when it succeeded: a per-pod
        # error is itself evidence, and it is lost if the index is conditional on success.
        (run_dir / "logs" / "index.json").write_text(
            json.dumps([entry.to_dict() for entry in collection.logs], indent=2), encoding="utf-8"
        )
    return collection


def _known_lines(records: list[dict], profile: Profile | None) -> list[str]:
    """What the profile's `known_failures` said about the failures, as part of the counts."""
    known = known_summary(records, profile.known_failures if profile else ())
    out = []
    if known["failing"]:
        issues = ", ".join(f"{issue} ×{n}" for issue, n in known["issues"].items())
        out.append(
            f"- known failures (fix pending, counted in fail/error above): "
            f"{known['failing']} ({issues})"
        )
    if known["fix_expected"]:
        out.append(f"- fix expected on this build, still failing: {known['fix_expected']}")
    for entry in known["unmatched"]:
        out.append(
            f"- known_failures entry matched no failure: {entry['issue']} ({entry['check']})"
        )
    return out


def _per_layer_counts(records: list[dict], order: list[str]) -> list[str]:
    """One tally per layer, outermost first, with skip classes named.

    A single total hides that a gateway skipped a third of the suite as undeclared while the
    engine ran all of it. Omitted for a single-layer run, where the run totals say the same
    thing.
    """
    if len(order) < 2:
        return []
    lines = ["## per layer", ""]
    for name in order:
        mine = [r for r in records if r.get("ingress") == name]
        if not mine:
            continue
        passed = sum(1 for r in mine if r["outcome"] == "pass")
        failed = sum(1 for r in mine if r["outcome"] in ("fail", "error"))
        skips: dict[str, int] = {}
        for record in mine:
            if record["outcome"] == "skip":
                why = record.get("skip_class") or "unclassified"
                skips[why] = skips.get(why, 0) + 1
        detail = ", ".join(f"{count} {why}" for why, count in sorted(skips.items()))
        skipped = f", {sum(skips.values())} skipped ({detail})" if skips else ""
        lines.append(f"- **{name}** — {passed} passed, {failed} failed{skipped}")
    lines.append("")
    return lines


def _delta_section(grid: dict[str, dict[str, str]], order: list[str]) -> list[str]:
    """Cases whose outcome changed between layers, which is where a layer's effect shows.

    Skips are excluded deliberately: `skip:undeclared` means the case did not run at that
    layer, not that the layer behaved differently, and treating the two alike would report
    every surface a gateway does not expose as a regression.

    Returns nothing at all for a single-layer run: one column cannot diverge from itself.
    """
    if len(order) < 2:
        return []

    header = ["## what changed between layers", ""]
    diverged: list[tuple[str, dict[str, str]]] = []
    for case in sorted(grid):
        cells = {name: grid[case].get(name) for name in order if grid[case].get(name)}
        ran = {name: cell for name, cell in cells.items() if not cell.startswith("skip")}
        # The verdict alone: `fail:known` at one layer and `fail` at another is the same
        # answer, labelled differently by an entry scoped to one ingress.
        if len(ran) > 1 and len({cell.split(":")[0] for cell in ran.values()}) > 1:
            diverged.append((case, ran))

    if not diverged:
        return [
            *header,
            "No case changed result between layers. Every case that ran at more than one "
            "layer got the same answer from each.",
            "",
        ]

    present = [name for name in order if any(name in ran for _, ran in diverged)]
    width = max(len(case) for case, _ in diverged)
    rows = [
        "| " + "case".ljust(width) + " | " + " | ".join(present) + " |",
        "|" + "-" * (width + 2) + "|" + "|".join("-" * (len(n) + 2) for n in present) + "|",
    ]
    for case, ran in diverged:
        cells = " | ".join((ran.get(name) or "-").ljust(len(name)) for name in present)
        rows.append(f"| {case.ljust(width)} | {cells} |")
    return [
        *header,
        f"{len(diverged)} case(s) got a different answer from different layers. "
        "That difference is the layer's contribution.",
        "",
        *rows,
        "",
    ]


def _attribution_section(ingresses: list[str], profile: Profile | None = None) -> list[str]:
    """The attribution reading, or an honest statement that this run cannot make one.

    The ladder is a comparison between columns, and it is more than that: it claims the columns
    sit in a known order. Both claims have to hold. With one ingress there is nothing to
    compare; with several whose relationship the profile never declared there is a comparison
    but no ordering to read it against, and inventing one would assert a nesting the profile
    does not state. Naming the layers that reported is always fine -- the rest of this summary
    has to label their columns anyway -- what must not be invented is which fronts which.
    """
    if not ingresses:
        # No ingress reported at all: every case skipped before a client was built. Saying "one
        # ingress was exercised" here would be worse than saying nothing.
        return [
            "## attribution",
            "",
            "**Not available in this run.** No ingress reported a result, so nothing was",
            "exercised and there is nothing to attribute. Every case skipped before a client was",
            "built -- check the skip classes in the table above for which gap applies.",
            "",
        ]

    if len(ingresses) == 1:
        return _no_second_column(ingresses[0])

    # A results file can be summarised against a profile that declares none, or only one, of
    # the ingresses that reported -- and `_summarise` may be called with no profile at all.
    # Either way there is no declared order over these layers, so there is no ladder to build.
    declared = [name for name in profile.chain() if name in ingresses] if profile else []
    if len(declared) < 2:
        return _no_declared_order(ingresses, declared, profile is not None)

    rows = [f"| {declared[-1]} fails | backend, model template, or fixture |"]
    for outer, inner in zip(declared, declared[1:], strict=False):
        rows.append(
            f"| {inner} passes, {outer} fails | "
            f"{outer} parsing, mutation, routing, auth, or timeout |"
        )
    return [
        "## attribution (a hypothesis, not a proof)",
        "",
        "chain, outermost first: " + " -> ".join(declared),
        "",
        "| pattern | suggests |",
        "| --- | --- |",
        *reversed(rows),
        "",
        "The ladder assumes the layers are in series and otherwise identical. They are "
        "not: an engine reached directly serves one replica while an outer layer may "
        "load-balance across many, and in some fleets the reasoning and tool parsers are "
        "configured on only one role. Check `served_by` in results.jsonl before "
        "attributing a difference to a layer.",
        "",
    ]


def _no_second_column(ingress: str) -> list[str]:
    """Honest statement that this run cannot make an attribution: just one column to show.

    Exactly one ingress reported. That is the only case this text describes -- a run where
    several reported but their order is unknown has a second column, and saying otherwise
    contradicts the grid, the per-layer tally and the delta table printed just above it.
    """
    return [
        "## attribution",
        "",
        f"**Not available in this run.** One ingress was exercised (`{ingress}`), so there is no",
        "second column to compare against and no layer a failure can be isolated to. A failure",
        f"here says the exchange did not hold at `{ingress}`; it does not say which component",
        "behind that address is responsible.",
        "",
        "That is a narrower question, not a lesser result. Declaring more than one ingress in the",
        "profile and rerunning is what turns a failure into an attribution.",
        "",
        "`served_by` is still recorded on every result, so where the layer reveals which backend",
        "answered, the evidence is kept even though this run cannot act on it.",
        "",
    ]


def _no_declared_order(ran: list[str], declared: list[str], has_profile: bool) -> list[str]:
    """Several layers reported, but nothing declares how they are stacked.

    Naming the layers that ran is not the thing the attribution rule forbids: they produced the
    columns above, and the grid, the tally and the delta table all name them already. What is
    forbidden is the ladder, because a ladder asserts an ordering -- which layer sits in front
    of which -- and that relationship is exactly what an undeclared ingress does not supply.
    """
    names = ", ".join(f"`{name}`" for name in ran)
    if not has_profile:
        why = (
            "this summary was written without a profile, so nothing here declares how those "
            "layers are stacked"
        )
    elif not declared:
        why = "this profile declares none of them, so nothing says how they are stacked"
    else:
        why = (
            f"this profile declares only {', '.join(f'`{n}`' for n in declared)} among them, "
            "so nothing says where the others sit relative to it"
        )
    return [
        "## attribution",
        "",
        f"**Not available in this run.** {len(ran)} ingresses reported results ({names}), but",
        f"{why}.",
        "",
        "Attribution is read off a chain, outermost layer inwards: it is the order that turns "
        '"A passed, B failed" into a statement about what B adds. Without a declared order '
        "there is no chain to read, and guessing one from the results would assert a layering "
        "nothing in this run declared.",
        "",
        "The columns above are still real results, each for the address it names. Declaring "
        "these ingresses in the profile -- with their `kind`, which is what orders the chain -- "
        "is what turns them into an attribution.",
        "",
        "`served_by` is still recorded on every result, so where a layer reveals which backend",
        "answered, the evidence is kept even though this run cannot act on it.",
        "",
    ]


def _write_reproduce(args, profile, run_dir, setup_commands) -> None:
    key_envs = sorted(
        {ingress.auth.key_env for ingress in profile.ingresses if ingress.auth.key_env}
    )
    argv = reproduce.rewrite_argv(["llmdv", *sys.argv[1:]], "profile.used.yaml")
    reproduce.write(
        run_dir,
        profile_name=profile.name,
        profile_digest=loader.contract_digest(args.profile),
        argv=argv,
        setup_commands=setup_commands,
        key_envs=key_envs,
    )


def _unknown_selectors(profile, args) -> list[str]:
    """Names given to --ingress/--client/--surface that this profile does not define."""
    from ..surfaces.base import KNOWN_SURFACES

    problems: list[str] = []
    for ingress_id in args.ingress or []:
        if ingress_id not in profile.ingress_ids:
            problems.append(
                f"--ingress {ingress_id!r} is not in profile {profile.name!r}; "
                f"it declares: {', '.join(profile.ingress_ids)}"
            )
    declared_clients = [client.id for client in profile.clients]
    for client_id in args.client or []:
        if client_id not in declared_clients:
            problems.append(
                f"--client {client_id!r} is not in profile {profile.name!r}; "
                f"it declares: {', '.join(declared_clients) or 'no clients'}"
            )
    for surface in args.surface or []:
        if surface not in KNOWN_SURFACES:
            problems.append(
                f"--surface {surface!r} is not a known surface; known: "
                f"{', '.join(sorted(KNOWN_SURFACES))}"
            )
    return problems


# pytest's own exit codes. Collapsing these into "tests failed" would report a llmdv defect or a
# bad selection as a fleet verdict, which is exactly the confusion the exit codes exist to end.
def _map_pytest_exit(code: int, interrupted: bool = False) -> int:
    if code == 0:
        return OK
    if code == 1:
        return TEST_FAILURES
    if code == 2 and interrupted:
        log.warning("the run was interrupted before it completed; its results are partial")
        return INTERRUPTED
    if code == 2:
        log.error(
            "pytest stopped on collection errors -- this is a llmdv defect, not a fleet result"
        )
        return INTERNAL
    if code == 3:
        log.error("pytest hit an internal error -- this is a llmdv defect, not a fleet result")
        return INTERNAL
    if code == 4:
        log.error("pytest usage error in the arguments llmdv built")
        return USAGE
    if code == 5:
        log.warning("no tests were collected; check --ingress/--client/--surface and -k/-m")
        return USAGE
    return INTERNAL


def _build_pytest_argv(
    args: argparse.Namespace, run_dir: Path, run_id: str, started_at: str | None = None
) -> list[str]:
    argv = [
        str(SUITES_DIR),
        "--llmdv-profile",
        args.profile,
        "--llmdv-run-dir",
        str(run_dir),
        "--llmdv-run-id",
        run_id,
        # Minimal native output: the structured path is results.jsonl, and whatever pytest
        # still prints is redirected onto a logger rather than the terminal.
        "-q",
        "--no-header",
        "--no-summary",
        "-o",
        "console_output_style=classic",
    ]
    # Never parallel, even where pytest-xdist is installed (the `dev` extra installs it for
    # llmdv's own offline tests). Blocking the plugin also turns an -n smuggled in through
    # PYTEST_ADDOPTS into a usage error rather than concurrent requests against a live fleet.
    argv += ["-p", "no:xdist"]
    # Only load the plugin explicitly when the pytest11 entry point has not already done it --
    # registering it twice is an error, and running from a source checkout is the case that
    # needs it.
    if not _plugin_is_registered():
        argv[1:1] = ["-p", "llmdv.plugin"]

    argv += ["--llmdv-evidence", args.evidence]
    if args.transcript_cap is not None:
        argv += ["--llmdv-transcript-cap", str(args.transcript_cap)]
    if getattr(args, "artifact_sink", None):
        argv += ["--llmdv-artifact-sink", args.artifact_sink]
    if getattr(args, "verbose", 0) >= 1:
        argv += ["--llmdv-show-checks"]
    for ingress_id in args.ingress:
        argv += ["--llmdv-ingress", ingress_id]
    for client_id in args.client:
        argv += ["--llmdv-client", client_id]
    for surface in args.surface:
        argv += ["--llmdv-surface", surface]
    if args.kubeconfig:
        argv += ["--llmdv-kubeconfig", str(args.kubeconfig)]
    if args.context:
        argv += ["--llmdv-context", args.context]
    if getattr(args, "kube_cli", None) is not None:
        argv += ["--llmdv-kube-cli", args.kube_cli]
    if started_at:
        argv += ["--llmdv-started-at", started_at]
    if args.keyword:
        argv += ["-k", args.keyword]
    if args.marker:
        argv += ["-m", args.marker]

    extra = [item for item in (getattr(args, "pytest_args", None) or []) if item != "--"]
    return argv + extra


# pytest-xdist's options that would start workers or choose how cases are spread over them.
_PARALLEL_OPTIONS = ("--numprocesses", "--maxprocesses", "--dist", "--tx")


def _parallel_args(extra: list[str]) -> list[str]:
    """The passthrough arguments that ask for parallel workers, which verify refuses."""
    return [
        item
        for item in extra
        if item == "-n"
        or (item.startswith("-n") and not item.startswith("--"))
        or item.split("=", 1)[0] in _PARALLEL_OPTIONS
    ]


def _plugin_is_registered() -> bool:
    """Whether llmdv's pytest plugin will be auto-loaded from its entry point."""
    try:
        from importlib.metadata import entry_points

        return any(ep.module == "llmdv.plugin" for ep in entry_points(group="pytest11"))
    except Exception:  # noqa: BLE001 -- fall back to loading it explicitly
        return False


def _run_pytest(argv: list[str], raw: bool, setup_commands: dict[str, list[str]]) -> int:
    import pytest

    class _Attach:
        """Hands the resolved setup commands to the plugin for evidence bundles."""

        def pytest_configure(self, config):
            config._llmdv_setup_commands = setup_commands

    if raw:
        return int(pytest.main(argv, plugins=[_Attach()]))

    stdout = LoggerWriter("llmdv.pytest", logging.DEBUG)
    stderr = LoggerWriter("llmdv.pytest", logging.WARNING)
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = int(pytest.main(argv, plugins=[_Attach()]))
    stdout.flush()
    stderr.flush()
    return code


def _write_run_metadata(
    run_dir, profile, args, setup_commands, outcome: Outcome, collection=None, kube=None
) -> None:
    import platform

    versions = {"python": sys.version.split()[0], "platform": platform.platform()}
    for name in ("httpx", "openai", "anthropic", "pytest", "yaml"):
        try:
            module = __import__(name)
            versions[name] = getattr(module, "__version__", "unknown")
        except ImportError:
            versions[name] = "not installed"

    (run_dir / "env.json").write_text(
        json.dumps(
            {
                "llmdv_version": __version__,
                "git_sha": os.environ.get("LLMDV_GIT_SHA") or None,
                "run_id": outcome.run_id,
                "labels": outcome.labels,
                "started": outcome.started,
                "versions": versions,
                "profile_path": str(Path(args.profile).resolve()),
                "vantage": profile.vantage.value,
                "kube_cli": Path(kube.binary).name if kube and kube.binary else None,
                "kube_cli_requested": getattr(args, "kube_cli", None),
                "setup_commands": setup_commands,
                "ingress_models": {
                    ingress.id: {
                        "model": asdict(ingress.model),
                        "capabilities": asdict(ingress.capabilities),
                    }
                    for ingress in profile.ingresses
                    if not args.ingress or ingress.id in args.ingress
                },
                # So a bundle read months later can tell "nothing failed, so we did not
                # collect" from "we were not allowed to".
                "pod_logs": {
                    "collected": len(getattr(collection, "ok", [])),
                    "reason": getattr(collection, "reason", None),
                    "offloaded": sum(
                        1
                        for entry in getattr(collection, "ok", [])
                        if entry.artifact
                        and entry.artifact.get("offload", {}).get("status") == "stored"
                    ),
                    "artifact_sink": getattr(args, "artifact_sink", None),
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_records(run_dir: Path) -> list[dict]:
    """Every case's record, or an empty list if the suite never got far enough to write one."""
    results = run_dir / "results.jsonl"
    if not results.is_file():
        return []
    return [json.loads(line) for line in results.read_text().splitlines() if line.strip()]


def _pod_log_section(collection) -> list[str]:
    """How to read the logs, or why there are none. Never both, and never neither.

    This section used to be unconditional, so a run with no cluster access still explained how
    to correlate pod logs against `exchanges.jsonl` -- sending the reader to look for files
    that were never going to be there.
    """
    if not getattr(collection, "ok", []):
        reason = getattr(collection, "reason", None) or "no successful log reads were recorded"
        return [
            "### Pod logs",
            "",
            f"None were collected: {reason}.",
            "",
            "This run carries our side of every exchange and no account from the components",
            "that served it. A failure below says what the client saw, not what the layer did.",
            "",
        ]
    return [
        "### Correlating the pod logs",
        "",
        "`logs/` holds each layer's own account for the run window. Correlate by **timestamp**,",
        "not by request id: the llm-d EPP mints its own `x-request-id` rather than propagating",
        "the client's, which is why the profile declares that header `ignored` at the router.",
        "`exchanges.jsonl` carries the wall-clock timing of every request to line them up against.",
        "",
    ]


def layer_order(records: list[dict], profile: Profile | None = None) -> list[str]:
    """The ingresses that reported, outermost first.

    Shared by summary.md and the closing banner so the two read in the same direction. An
    ingress the profile does not declare keeps its first-seen position after the declared
    ones, rather than being dropped: it ran, so it has a column.
    """
    seen: list[str] = []
    for record in records:
        if record.get("ingress") and record["ingress"] not in seen:
            seen.append(record["ingress"])
    if profile is None:
        return seen
    declared = [name for name in profile.chain() if name in seen]
    return declared + [name for name in seen if name not in declared]


def _summarise(
    run_dir: Path, records: list[dict], collection=None, profile: Profile | None = None
) -> tuple[list[dict], list[str]]:
    """Write summary.md and return (records, the rendered table) for the closing banner.

    The attribution reading is labelled a suggestion on purpose. The ladder assumes the layers
    are in series and otherwise identical, and they are not -- see llmdv/ingress/base.py. It is
    omitted entirely whenever no ordering can be established: for a single-ingress run, where
    there is no second column to compare, and for a run whose profile declares fewer than two of
    the ingresses that reported, where the order between them is unknown.
    """
    if not records:
        return [], []

    ingresses = layer_order(records, profile)

    grid: dict[str, dict[str, str]] = {}
    for record in records:
        case_id = record.get("case_id") or record.get("nodeid", "")
        if record.get("variant"):
            case_id = f"{case_id}[{record['variant']}]"
        cell = record["outcome"]
        if cell == "skip" and record.get("skip_class"):
            cell = f"skip:{record['skip_class']}"
        elif cell in ("fail", "error") and (record.get("known_failure") or {}).get("status"):
            # `fail:known` reads beside `skip:<class>`: the verdict, then what is known of it.
            cell = f"{cell}:{record['known_failure']['status']}"
        grid.setdefault(case_id, {})[record.get("ingress") or "-"] = cell

    case_width = max((len(case) for case in grid), default=10)
    # Width from the widest CELL, not just the header: "skip:requires_instrumentation" is 29
    # characters and would otherwise push the row past its own separator line.
    col_widths = [
        max([len(ingress)] + [len(row.get(ingress, "-")) for row in grid.values()])
        for ingress in ingresses
    ]

    table = [
        "| "
        + "case".ljust(case_width)
        + " | "
        + " | ".join(i.ljust(w) for i, w in zip(ingresses, col_widths, strict=True))
        + " |",
        "|" + "-" * (case_width + 2) + "|" + "|".join("-" * (w + 2) for w in col_widths) + "|",
    ]
    for case in sorted(grid):
        pairs = zip(ingresses, col_widths, strict=True)
        cells = [grid[case].get(i, "-").ljust(w) for i, w in pairs]
        table.append(f"| {case.ljust(case_width)} | " + " | ".join(cells) + " |")

    counts: dict[str, int] = {}
    for record in records:
        counts[record["outcome"]] = counts.get(record["outcome"], 0) + 1

    lines = [
        "# llmdv run summary",
        "",
        f"profile: {records[0].get('profile')}",
        f"digest:  {records[0].get('profile_digest')}",
        f"run_id:  {records[0].get('run_id')}",
        "",
        *table,
        "",
        "## counts",
        "",
        *[f"- {outcome}: {count}" for outcome, count in sorted(counts.items())],
        *_known_lines(records, profile),
        "",
        *_per_layer_counts(records, ingresses),
        *_delta_section(grid, ingresses),
        *_pod_log_section(collection),
        *_attribution_section(ingresses, profile),
    ]
    (run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    return records, table
