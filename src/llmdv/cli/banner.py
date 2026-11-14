"""The run banner, the phase headings, and the closing summary.

Three questions should be answerable without reading a log: what is about to run and where,
what is happening now, and where the results went. The banner answers the first, the phase
headings and per-case lines answer the second, and the summary answers the third.

Banner and summary are primary output (stdout, never suppressed by --log-level), because they
are the report. Phase headings and per-case progress are diagnostics (stderr, under
--log-level), because they are commentary on work in flight -- so `llmdv verify > report.txt`
keeps the report and leaves the play-by-play on the terminal.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

from .. import __version__
from ..kube import KubeContext
from ..log.setup import emit, get_logger
from ..profile.known import FIX_EXPECTED, KNOWN
from ..profile.schema import AuthType, Profile
from ..surfaces import implemented
from . import style
from .contract_display import contract_lines

log = get_logger("cli.banner")

WIDTH = 78
KEY = 10

ARTIFACTS = (
    ("results.jsonl", "one record per case, with the checks behind each verdict"),
    ("exchanges.jsonl", "every HTTP request and response, redacted -- the audit trail"),
    (
        "summary.md",
        "per-ingress table, and an attribution reading where there are layers to compare",
    ),
    ("llmdv.log", "full DEBUG transcript: every check and exchange, redacted"),
    ("env.json", "llmdv/python/SDK versions and the resolved setup"),
    ("reproduce.sh", "re-runs this exact verification"),
    ("profile.used.yaml", "the profile verbatim; it carries no secrets by design"),
    ("logs/", "pod logs for the run window, per layer"),
    ("cases/", "per-case evidence bundles (--evidence all)"),
    ("failures/", "per-failure evidence: verdict.txt, request, response, repro.sh"),
    ("dashboard.html", "every test with its evidence and rerun command, in one page (--analyze)"),
)


def _palette() -> style.Palette:
    return style.Palette.for_stream(style.rich_stdout())


def _rule(width: int = WIDTH) -> None:
    emit(style.rule_char(style.rich_stdout()) * width)


def _kv(key: str, value: str, palette: style.Palette | None = None) -> None:
    pal = palette or _palette()
    emit(f"  {pal.paint(key.ljust(KEY), pal.dim)}{value}")


def start(
    profile: Profile,
    profile_path: str,
    selected_ingresses: list[str],
    args: Any,
    run_dir: Path,
    warnings: list[str],
    kube: KubeContext | None = None,
) -> None:
    """Print what is about to run, where, and what will not run."""
    pal = _palette()
    sep = style.separator(style.rich_stdout())

    emit("")
    emit(
        f"  {pal.paint(f'llmdv {__version__}', pal.bold)}  verify"
        f"{sep}{profile.name}{sep}{len(selected_ingresses)} ingress"
        f"{'es' if len(selected_ingresses) != 1 else ''}"
    )
    emit("")

    _kv("profile", f"{profile_path}", pal)
    if kube is not None and any(
        (
            profile.environment.cluster,
            profile.environment.namespace,
            profile.environment.context,
            profile.environment.kubeconfig,
        )
    ):
        choice = (
            kube.display_prefix()[0]
            if kube.available
            else f"unavailable ({kube.unavailable_reason})"
        )
        _kv("kube CLI", choice, pal)
    if profile.environment.namespace or profile.environment.cluster:
        where = "/".join(
            x for x in (profile.environment.cluster, profile.environment.namespace) if x
        )
        _kv("fleet", f"{where}{sep}vantage {profile.vantage.value}", pal)

    reasoning = profile.model.reasoning
    mode = (
        f"parser {reasoning.parser}"
        if reasoning.parser
        else f"inline <{reasoning.inline_tag}>"
        if reasoning.is_inline
        else "none declared"
    )
    model_label = (
        "defaults"
        if any(
            i.model != profile.model or i.capabilities != profile.capabilities
            for i in profile.ingresses
            if i.id in selected_ingresses
        )
        else "model"
    )
    _kv(
        model_label,
        f"{profile.model.public_id}{sep}reasoning {mode}{sep}"
        f"tools {profile.model.tools.parser or 'none'}",
        pal,
    )
    _kv("output", str(run_dir), pal)
    if getattr(args, "remote_output", None):
        _kv("", f"publish to {args.remote_output}", pal)

    emit("")
    built = implemented()
    for index, ingress_id in enumerate(selected_ingresses):
        ingress = profile.ingress(ingress_id)
        auth = ingress.auth.type.value
        if ingress.auth.type is not AuthType.NONE:
            auth += f" (${ingress.auth.key_env})"
        runnable = ", ".join(sorted(set(ingress.surfaces) & built)) or "nothing"
        label = "ingress" if index == 0 else ""
        emit(
            # The pad aligns the common case; the trailing space is what keeps a longer URL
            # from running straight into the kind, as an external Route's hostname does.
            f"  {pal.paint(label.ljust(KEY), pal.dim)}{ingress.id:<9}{ingress.base_url:<32} "
            f"{pal.paint(ingress.kind.value, pal.dim)}"
        )
        emit(f"  {' ' * KEY}{'':<9}{pal.paint(f'auth {auth}{sep}drives {runnable}', pal.dim)}")
        if ingress.model != profile.model or ingress.capabilities != profile.capabilities:
            for line in contract_lines(ingress.model, ingress.capabilities):
                emit(f"  {' ' * KEY}{'':<9}{line}")

    undrivable = sorted(
        {s for i in selected_ingresses for s in profile.ingress(i).surfaces} - built
    )
    if undrivable:
        emit("")
        _kv(
            "skipping",
            pal.paint(
                f"{', '.join(undrivable)}  (declared by the fleet, not drivable by llmdv "
                f"{__version__})",
                pal.dim,
            ),
            pal,
        )

    filters = _describe_filters(args)
    if filters:
        _kv("filters", filters, pal)
    for warning in warnings:
        _kv("note", warning, pal)
    emit("")


def _plural(word: str, count: int) -> str:
    if count == 1:
        return word
    return f"{word}es" if word.endswith(("s", "x", "ch", "sh")) else f"{word}s"


def phase(name: str, size: int, unit: str = "case") -> None:
    """A phase heading on the diagnostics stream, matching the ones the plugin emits."""
    rich = style.rich_stderr()
    palette = style.Palette.for_stream(rich)
    sep = "\u00b7" if rich else "-"
    label = f"{name} {sep} {size} {_plural(unit, size)}"
    bar = style.rule_char(rich)
    log.info("")
    log.info("%s", palette.paint(f"{bar * 2} {label} {bar * max(4, 64 - len(label))}", palette.dim))


def phase_line(verdict: str, left: str, middle: str, right: str) -> None:
    """One row inside a phase section, aligned with the plugin's case lines."""
    palette = style.Palette.for_stream(style.rich_stderr())
    log.info(
        "   %s %-8s %-52s %s",
        style.verdict("pass" if verdict == "ok" else verdict, palette, width=5),
        palette.paint(left, palette.dim),
        middle[:52],
        palette.paint(right, palette.dim),
    )


def _describe_filters(args: Any) -> str:
    parts = []
    for name in ("ingress", "client", "surface"):
        values = getattr(args, name, None) or []
        if values:
            parts.append(f"--{name} {', '.join(values)}")
    for name, flag in (("keyword", "-k"), ("marker", "-m")):
        value = getattr(args, name, None)
        if value:
            parts.append(f"{flag} {value!r}")
    return style.separator(style.rich_stdout()).join(parts)


def finish(
    run_dir: Path,
    records: list[dict],
    table: list[str],
    elapsed_s: float,
    published: str | None = None,
    collected: Any = None,
    layers: list[str] | None = None,
    known_unmatched: list[dict] | None = None,
) -> None:
    """Print the outcome table, the counts, every failure, and where it all landed."""
    pal = _palette()
    sep = style.separator(style.rich_stdout())

    counts: dict[str, int] = {}
    for record in records:
        counts[record["outcome"]] = counts.get(record["outcome"], 0) + 1

    failures = [r for r in records if r["outcome"] in ("fail", "error")]
    skips: dict[str, int] = {}
    for record in records:
        if record["outcome"] == "skip":
            key = record.get("skip_class") or "unclassified"
            skips[key] = skips.get(key, 0) + 1

    emit("")
    _rule()
    # Zero cases is not a pass. Reporting "PASSED | 0 cases" for a selector that matched nothing
    # is the most dangerous output this tool could produce: it is a green verdict over an empty
    # set, and in CI it would read as coverage.
    if not records:
        emit(
            f"  {pal.paint('NO CASES RAN', pal.yellow + pal.bold)}{sep}"
            f"nothing matched the selection{sep}{elapsed_s:.1f}s"
        )
        _rule()
        emit("")
        _kv("results", str(run_dir), pal)
        emit("")
        return

    failed = bool(failures)
    headline = "FAILED" if failed else "PASSED"
    colour = pal.red if failed else pal.green
    # The count must say what it counts. "FAILED | 69 cases" reads as 69 failures when 69 is the
    # total and 7 of them failed, and "PASSED | 69 cases" reads as 69 passes when 28 skipped.
    # This line is the one people quote, so it carries its own breakdown rather than relying on
    # the per-outcome lines further down.
    total = len(records)
    noun = "case" if total == 1 else "cases"
    failed_total = counts.get("fail", 0) + counts.get("error", 0)
    skipped_total = sum(skips.values())
    known_n = sum(1 for r in failures if _known_status(r) == KNOWN)
    expected_n = sum(1 for r in failures if _known_status(r) == FIX_EXPECTED)
    if failed:
        shape = f"{failed_total} of {total} {noun} failed"
        if known_n:
            # Still failed, and still counted: the fleet is broken. But the reader who quotes
            # this line should not have to open each failure to learn the team already knew.
            shape += f" ({known_n} known, fix pending)"
    elif skipped_total:
        shape = f"{total - skipped_total} of {total} {noun} passed, {skipped_total} skipped"
    else:
        shape = f"{total} {noun} passed"
    emit(f"  {pal.paint(headline, colour + pal.bold)}{sep}{shape}{sep}{elapsed_s:.1f}s")
    _rule()
    emit("")

    if table:
        for line in table:
            emit(f"  {line}")
        emit("")

    passed = counts.get("pass", 0)
    failed_n = counts.get("fail", 0) + counts.get("error", 0)
    emit(f"  {pal.paint('passed'.ljust(KEY), pal.dim)}{pal.paint(str(passed), pal.green)}")
    known_detail = ""
    if known_n or expected_n:
        parts = []
        if known_n:
            parts.append(f"{known_n} known, fix pending")
        if expected_n:
            parts.append(f"{expected_n} fix expected on this build, still failing")
        known_detail = f"   {pal.paint('(' + '; '.join(parts) + ')', pal.dim)}"
    emit(
        f"  {pal.paint('failed'.ljust(KEY), pal.dim)}"
        f"{pal.paint(str(failed_n), pal.red if failed_n else '')}{known_detail}"
    )
    skip_detail = ""
    if skips:
        breakdown = ", ".join(f"{count} {name}" for name, count in sorted(skips.items()))
        skip_detail = f"   {pal.paint('(' + breakdown + ')', pal.dim)}"
    emit(f"  {pal.paint('skipped'.ljust(KEY), pal.dim)}{counts.get('skip', 0)}{skip_detail}")

    # Per layer, outermost first. Omitted for one layer, where the totals above already say it
    # and a second identical tally only invites the reader to look for a difference. The run
    # totals answer "did this fleet hold"; this answers "which layer did not".
    if layers and len(layers) > 1:
        emit("")
        emit(f"  {pal.paint('per layer', pal.dim)}")
        width = max(len(name) for name in layers)
        for name in layers:
            mine = [r for r in records if r.get("ingress") == name]
            if not mine:
                continue
            passed_n = sum(1 for r in mine if r["outcome"] == "pass")
            failed_l = sum(1 for r in mine if r["outcome"] in ("fail", "error"))
            classes: dict[str, int] = {}
            for record in mine:
                if record["outcome"] == "skip":
                    why = record.get("skip_class") or "unclassified"
                    classes[why] = classes.get(why, 0) + 1
            detail = ", ".join(f"{n} {why}" for why, n in sorted(classes.items()))
            tail = f", {sum(classes.values())} skipped" if classes else ""
            line = (
                f"    {name.ljust(width)}  {pal.paint(str(passed_n), pal.green)} passed, "
                f"{pal.paint(str(failed_l), pal.red if failed_l else '')} failed{tail}"
            )
            emit(line + (f"   {pal.paint('(' + detail + ')', pal.dim)}" if classes else ""))

    if failures:
        emit("")
        emit(f"  {pal.paint('failures', pal.red + pal.bold)}")
        for index, record in enumerate(failures, start=1):
            case = record.get("case_id") or record.get("nodeid", "?")
            if record.get("variant"):
                case = f"{case}[{record['variant']}]"
            emit(
                f"    {index}. {pal.paint(case, pal.bold)}  "
                f"{pal.paint(record.get('ingress') or '-', pal.dim)}"
            )
            # Wrapped rather than truncated: the finding is the point of this block, and cutting
            # it mid-word forces a trip to the log for the one line that matters.
            reason = (record.get("reason") or "").strip().splitlines()
            if reason:
                for line in textwrap.wrap(reason[0], width=WIDTH - 9)[:3]:
                    emit(f"       {line}")
            if record.get("evidence"):
                emit(f"       {pal.paint('evidence: ' + record['evidence'], pal.dim)}")
            known = record.get("known_failure") or {}
            if _known_status(record) == KNOWN:
                emit(f"       {pal.paint(_known_line(known), pal.dim)}")
            elif _known_status(record) == FIX_EXPECTED:
                emit(f"       {pal.paint(_fix_expected_line(known), pal.yellow)}")

    by_issue = _known_by_issue(failures)
    if by_issue or known_unmatched:
        emit("")
        emit(
            f"  {pal.paint('known failures', pal.bold)}  "
            f"{pal.paint('declared in the profile; counted as failed above', pal.dim)}"
        )
        for issue, (count, fix) in by_issue.items():
            tail = f"  {pal.paint(fix, pal.dim)}" if fix else ""
            emit(f"    {issue:<40} ×{count}{tail}")
        for entry in known_unmatched or []:
            # Either the fix landed and the entry should go, or a -k left its cases out.
            note = f"matched no failure this run ({entry.get('check')})"
            emit(f"    {entry.get('issue', '?'):<40} {pal.paint(note, pal.dim)}")

    gathered = list(getattr(collected, "ok", []) or [])
    if gathered:
        emit("")
        emit(f"  {pal.paint('pod logs', pal.dim)}")
        for entry in gathered:
            note = ""
            if entry.truncated:
                stored = (
                    entry.artifact and entry.artifact.get("offload", {}).get("status") == "stored"
                )
                note = "  (truncated; whole copy offloaded)" if stored else "  (truncated)"
            emit(
                f"    {entry.ingress_id:<8} {entry.pod}/{entry.container}  "
                f"{pal.paint(f'{entry.lines} lines{note}', pal.dim)}"
            )
    elif getattr(collected, "reason", None):
        # Said once, here, rather than left for the reader to infer from a missing directory.
        emit("")
        emit(f"  {pal.paint('pod logs', pal.dim)}{' ' * (KEY - 8)}none: {collected.reason}")

    partial = sum(1 for record in records if record.get("partial_evidence"))
    if partial:
        emit("")
        emit(
            f"  {partial} verdict{'s' if partial != 1 else ''} rest on partial evidence; "
            "pass --artifact-sink to keep transcripts whole"
        )

    emit("")
    _kv("results", str(run_dir), pal)
    for name, description in ARTIFACTS:
        if name == "failures/" and not failures:
            continue
        if not (run_dir / name).exists():
            continue
        emit(f"  {' ' * KEY}{name:<19}{pal.paint(description, pal.dim)}")
    if published:
        _kv("published", published, pal)
    emit("")


def _known_status(record: dict) -> str | None:
    return (record.get("known_failure") or {}).get("status")


def _known_line(known: dict) -> str:
    fix = f"; fix: {known['fix']}" if known.get("fix") else ""
    return f"known: {known.get('issue')}{fix}"


def _fix_expected_line(known: dict) -> str:
    seen = ", ".join(f"{name}={value}" for name, value in (known.get("observed") or {}).items())
    return f"fix for {known.get('issue')} expected on this build ({seen}), still failing"


def _known_by_issue(failures: list[dict]) -> dict[str, tuple[int, str | None]]:
    out: dict[str, tuple[int, str | None]] = {}
    for record in failures:
        if _known_status(record) != KNOWN:
            continue
        known = record["known_failure"]
        issue = str(known.get("issue") or "?")
        count, fix = out.get(issue, (0, None))
        out[issue] = (count + 1, fix or known.get("fix"))
    return dict(sorted(out.items(), key=lambda kv: (-kv[1][0], kv[0])))
