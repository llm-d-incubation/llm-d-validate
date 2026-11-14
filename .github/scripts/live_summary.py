"""Write the GitHub job summary for a live run, as Markdown on stdout.

Usage: live_summary.py <run_dir> --profile P --gateway G --channel C --event E [--code N]
                       [--compare FILE]

Shows the verdict, the totals, a pass/fail/skip table per suite and the largest failure groups
(by first failing check, as `llmdv analyze` groups them).

The summary is visible to anyone who can read the repository's Actions. It must never carry a
secret, so the workflow runs this step with NO credentials in its environment: no AWS keys and
no gateway key, which makes it unable to print one whatever the run files contain. It reads
only the run directory, which llmdv has already redacted. As defence in depth, it also refuses
to print anything that looks like a credential (`sk-...`, `Bearer ...`, `AKIA...`), and
scrubs the endpoint hostnames, which are not secret but are cluster-identifying.

There is deliberately no link to the dashboard: a signed S3 link carries the access key ID and
grants read access to anyone holding it. The dashboard is published with the run; pull it as
`docs/end-to-end.md` describes.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

MEANING = {
    0: "every selected case passed",
    1: "the gateway failed cases: findings for triage",
    2: "usage or profile error",
    3: "preflight failed: gateway, key or bucket unreachable; nothing was tested",
    4: "llmdv defect",
    5: "verification passed but the run was not published",
    6: "interrupted; what finished is kept",
}
MAX_GROUPS = 12
# Credential shapes, matching llmdv's own dashboard scan. A hit is replaced, never printed.
CREDENTIAL = re.compile(
    r"\bsk-[A-Za-z0-9_-]{16,}|Bearer\s+(?!\$\{)(?!\*\*\*)[A-Za-z0-9._~+/-]{20,}|AKIA[0-9A-Z]{16}"
    # An AWS secret access key: exactly 40 base64 characters, standing alone.
    r"|(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])"
)


def hosts_to_hide() -> set[str]:
    hide = set()
    for name in ("GATEWAY_URL", "S3_URL"):
        url = os.environ.get(name)
        if url:
            parts = urlsplit(url)
            hide.update(v for v in (url, url.rstrip("/"), parts.netloc, parts.hostname) if v)
    return hide


def scrub(text: str, hide: set[str]) -> str:
    for value in sorted(hide, key=len, reverse=True):
        text = text.replace(value, "<endpoint>")
    return CREDENTIAL.sub("<redacted>", text)


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def read_text(path: Path | None) -> str:
    try:
        return path.read_text().strip() if path else ""
    except OSError:
        return ""


def read_results(run: Path) -> list[dict]:
    records = []
    try:
        for line in (run / "results.jsonl").read_text().splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return records


def failure_groups(run: Path) -> list[dict]:
    """The same groups `llmdv analyze` prints; empty if llmdv cannot be imported."""
    try:
        from llmdv.evidence import dashboard

        return dashboard.summary(run)["clusters"]
    except Exception:  # noqa: BLE001 -- a summary must never fail the job
        return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--profile", default="")
    parser.add_argument("--gateway", default="")
    parser.add_argument("--channel", default="")
    parser.add_argument("--event", default="")
    parser.add_argument("--code", default="")
    parser.add_argument("--compare", type=Path, default=None)
    args = parser.parse_args()
    run, hide = args.run_dir, hosts_to_hide()

    outcome = read_json(run / "outcome.json")
    started = "scheduled" if args.event == "schedule" else "on demand"
    out = [f"## E2E run on `{args.gateway or '?'}`: `{args.profile}`", ""]
    if not outcome:
        out.append(
            "No `outcome.json`: the run did not get far enough to write one. See the job log."
        )
        print(scrub("\n".join(out), hide))
        return 0

    v = outcome.get("verification") or {}
    code = v.get("exit_code")
    if code is None and str(args.code).isdigit():
        code = int(args.code)
    counts = collections.Counter(v.get("counts") or {})
    passed, failed, skipped = (
        counts.get("pass", 0),
        counts.get("fail", 0) + counts.get("error", 0),
        counts.get("skip", 0),
    )
    known = v.get("known") or {}
    known_note = f" ({known['failing']} known, fix pending)" if known.get("failing") else ""
    verdict = (
        "✅ **Passed**"
        if code == 0
        else f"❌ **{failed} failed**{known_note}"
        if code == 1
        else f"⚠️ **exit {code}**"
    )
    out += [
        f"{verdict}: {MEANING.get(code, 'no exit code recorded')}",
        "",
        "| Run | Started | Gateway | Folder | Passed | Failed | Skipped |",
        "| --- | --- | --- | --- | ---: | ---: | ---: |",
        (
            f"| `{outcome.get('run_id')}` | {started} | `{args.gateway}` | `{args.channel}` "
            f"| {passed} | {failed} | {skipped} |"
        ),
        "",
    ]

    if (run / "dashboard.html").is_file():
        dashboard_line = (
            "The dashboard is published with the run; "
            "pull it as `docs/end-to-end.md` describes."
        )
    else:
        dashboard_line = "No dashboard: start the run with `analyze=true` to publish one."

    records = read_results(run)
    if records:
        suites: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        for r in records:
            suites[str(r.get("case_id") or "?").split(".")[0]][r.get("outcome")] += 1
        body = [
            "### By suite",
            "",
            "| Suite | Passed | Failed | Skipped |",
            "| --- | ---: | ---: | ---: |",
        ]
        for name in sorted(suites, key=lambda n: (-(suites[n]["fail"] + suites[n]["error"]), n)):
            c = suites[name]
            body.append(f"| `{name}` | {c['pass']} | {c['fail'] + c['error']} | {c['skip']} |")
        groups = failure_groups(run)
        if groups:
            body += ["", f"### Largest failure groups ({len(groups)} in all)", ""]
            body += ["| Cases | First failing check | Examples |", "| ---: | --- | --- |"]
            for g in groups[:MAX_GROUPS]:
                examples = ", ".join(f"`{e}`" for e in (g.get("examples") or [])[:3])
                body.append(f"| {g['count']} | `{g['key']}` | {examples} |")
        if known.get("issues"):
            body += [
                "",
                "### Known failures, fix pending",
                "",
                "Declared in the profile's `known_failures` and counted as failed above.",
                "",
                "| Cases | Issue |",
                "| ---: | --- |",
            ]
            for issue, count in known["issues"].items():
                body.append(f"| {count} | {issue} |")
        if known.get("fix_expected"):
            body += [
                "",
                f"⚠️ {known['fix_expected']} known failure(s) still fail on the build their fix "
                "was expected in. They are counted as plain failures.",
            ]
        if known.get("unmatched"):
            names = ", ".join(f"{e.get('issue')} ({e.get('check')})" for e in known["unmatched"])
            body += ["", f"`known_failures` entries that matched no failure: {names}"]
        skips = v.get("skips") or {}
        if skips:
            body += [
                "",
                "Skips by class: " + ", ".join(f"`{k}` {n}" for k, n in sorted(skips.items())),
            ]
        out += body
    for problem in outcome.get("preflight_problems") or []:
        out.append(f"- preflight: {problem}")

    publication = outcome.get("publication") or {}
    where = publication.get("location") or publication.get("destination") or ""
    out += [
        "",
        f"Published: {publication.get('status') or 'no'}, `{where}`",
    ]
    out += ["", dashboard_line]
    comparison = read_text(args.compare)
    if comparison:
        # A four-backtick fence, so a triple backtick in the text cannot close it.
        out += [
            "",
            f"### Compared with the previous run in `{args.channel}/{args.gateway}/`",
            "",
            "````text",
            comparison,
            "````",
        ]
    print(scrub("\n".join(out), hide))
    return 0


if __name__ == "__main__":
    sys.exit(main())
