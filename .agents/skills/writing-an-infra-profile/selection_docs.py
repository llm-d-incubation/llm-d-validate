"""Write, or check, the "What `llmdv verify` selects" section of every profile folder's README.

For each runnable profile (any YAML under `profiles/` outside a `parts/` folder, `.local.yaml`
excluded) it composes the test sets through `extends`, finds the file that defines each set, and
runs `llmdv plan --json` offline for the default, `--test-set smoke`, `--all-test-sets` and
`--full-suite`. Profiles in one folder share one section, which lists every profile it covers.
It sends nothing to any stack.

    python .agents/skills/writing-an-infra-profile/selection_docs.py            # rewrite
    python .agents/skills/writing-an-infra-profile/selection_docs.py --check    # exit 1 if stale

It refuses to write a shared section when the profiles of one folder define different sets, or
when a planned suite has no pattern in the default: those need a section written by hand.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PROFILES = ROOT / "profiles"
HEADING = "## What `llmdv verify` selects"
SCOPES = {
    "plain": (),
    "smoke": ("--test-set", "smoke"),
    "all-test-sets": ("--all-test-sets",),
    "full-suite": ("--full-suite",),
}


def runnable() -> list[Path]:
    return sorted(
        p
        for p in PROFILES.rglob("*.yaml")
        if "parts" not in p.relative_to(PROFILES).parts and not p.name.endswith(".local.yaml")
    )


def extends_chain(path: Path) -> list[Path]:
    data = yaml.safe_load(path.read_text()) or {}
    parents = data.get("extends") or []
    parents = [parents] if isinstance(parents, str) else parents
    chain: list[Path] = []
    for parent in parents:
        chain += extends_chain((path.parent / parent).resolve())
    return chain + [path.resolve()]


def composed_sets(path: Path) -> tuple[dict, dict]:
    """The test sets after `extends`, and the file that last defined each."""
    sets: dict = {}
    sources: dict = {}
    for file in extends_chain(path):
        data = yaml.safe_load(file.read_text()) or {}
        if "test_sets" not in data:
            continue
        if data["test_sets"] is None:
            sets, sources = {}, {}
            continue
        for name, cases in data["test_sets"].items():
            if cases is None:
                sets.pop(name, None)
                sources.pop(name, None)
            else:
                sets[name], sources[name] = cases, file
    return sets, sources


def plan(path: Path, flags: tuple[str, ...]) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "llmdv.cli.main", "plan", "--profile", str(path), "--json", *flags],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"rc": result.returncode}
    document = json.loads(result.stdout)
    skips: collections.Counter = collections.Counter()
    for case in document["cases"]:
        if case["outcome"] != "run":
            skips[case["skip_class"] or "unclassified"] += 1
    counts = document["counts"]
    return {
        "total": counts["total"],
        "run": counts["run"],
        "skip": counts["skip"],
        "suites": sorted(counts["by_suite"]),
        "skips": dict(sorted(skips.items())),
    }


def describe(result: dict) -> str:
    if "rc" in result:
        return f"exits {result['rc']}"
    text = f"{result['total']} selected, {result['run']} run"
    if result["skip"]:
        classes = ", ".join(f"{n} {name}" for name, n in result["skips"].items())
        text += f", {result['skip']} skip: {classes}"
    return text


def collect() -> dict[Path, dict[Path, dict]]:
    profiles = runnable()
    jobs = [(p, scope, flags) for p in profiles for scope, flags in SCOPES.items()]
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        plans = list(pool.map(lambda job: plan(job[0], job[2]), jobs))
    folders: dict[Path, dict[Path, dict]] = collections.defaultdict(dict)
    for (path, scope, _), result in zip(jobs, plans, strict=True):
        entry = folders[path.parent].setdefault(path, {"plans": {}})
        entry["plans"][scope] = result
    for folder in folders.values():
        for path, entry in folder.items():
            entry["sets"], entry["sources"] = composed_sets(path)
    return folders


def section(folder: Path, entries: dict[Path, dict]) -> str:
    names = sorted(entries)
    sets = entries[names[0]]["sets"]
    if any(entries[p]["sets"] != sets for p in names):
        sys.exit(f"{folder}: its profiles define different test sets; write the section by hand")
    if "default" not in sets:
        sys.exit(f"{folder}: a profile has no default test set; write the section by hand")

    def rel(target: Path) -> str:
        return os.path.relpath(target, folder)

    sources = {p: entries[p]["sources"]["default"] for p in names}
    defining = set(sources.values())
    if len(defining) == 1 and next(iter(defining)) not in sources:
        src = rel(next(iter(defining)))
        where = f"defined once in [`{src}`]({src}) and used unchanged by every profile here"
    else:
        own = sorted(p for p, s in sources.items() if s == p)
        inherited = sorted((p, s) for p, s in sources.items() if s != p)
        where = "defined in " + (
            "each profile's own `test_sets:` (the copies are identical)"
            if len(own) > 1
            else f"`{own[0].name}`"
        )
        where += "".join(f"; `{p.name}` inherits it from `{rel(s)}`" for p, s in inherited)
    others = sorted(set(sets) - {"default"})
    guide = rel(ROOT / "docs/cli.md") + "#test-sets-and-coverage"
    out = [
        HEADING,
        "",
        f"The default is {where}."
        + (" Other sets: " + ", ".join(f"`{n}`" for n in others) + "." if others else ""),
        f"See [selection rules]({guide}) for smoke, full-library coverage and filters.",
        "",
    ]
    by_suite: dict[str, list[str]] = {}
    for pattern in sets["default"]:
        by_suite.setdefault(re.split(r"[.\[]", pattern, maxsplit=1)[0], []).append(pattern)
    planned = set(entries[names[0]]["plans"]["plain"].get("suites", ()))
    if "*" not in sets["default"] and planned != set(by_suite):
        sys.exit(f"{folder}: planned suites {sorted(planned)} differ from the default's patterns")
    out += [
        "<details>",
        "<summary>The default's case-ID patterns</summary>",
        "",
        "`*` selects the whole library; `suite.*` selects a suite. Other patterns select matching cases.",
        "",
        "| Suite | Patterns |",
        "| --- | --- |",
    ]
    for suite, patterns in by_suite.items():
        out.append(
            f"| {'All suites' if suite == '*' else f'`{suite}`'} | "
            + ", ".join(f"`{p}`" for p in patterns)
            + " |"
        )
    out += [
        "",
        "</details>",
        "",
        "<details>",
        "<summary>How many cases each scope selects, per profile</summary>",
        "",
        "Counted offline by `llmdv plan`, one per ingress and variant. *Run* cases can still skip "
        "during a run (for example `throttled`); *skip* cases are ruled out by the profile's "
        "declarations, with their skip class.",
        "",
    ]
    default_header = "Default" + ("" if others else " (= `--all-test-sets`)")
    extra = [] if not others else ["`--all-test-sets`"]
    out += [
        "| Profile | " + " | ".join([default_header, *extra]) + " | `smoke` | `--full-suite` |",
        "| --- | " + " | ".join("---" for _ in [default_header, *extra]) + " | --- | --- |",
    ]
    for p in names:
        plans = entries[p]["plans"]
        cells = [describe(plans["plain"])] + ([describe(plans["all-test-sets"])] if others else [])
        out.append(
            f"| `{p.name}` | "
            + " | ".join(cells)
            + f" | {describe(plans['smoke'])} | {describe(plans['full-suite'])} |"
        )
    out += [
        "",
        "</details>",
        "",
        "Preview individual cases and skip reasons with `llmdv plan --profile <profile>`.",
        "",
    ]
    return "\n".join(out)


def updated(readme: Path, text: str) -> str:
    """The README with its section replaced, or inserted before its second `## ` heading."""
    doc = readme.read_text() if readme.exists() else ""
    if HEADING in doc:
        start = doc.index(HEADING)
        end = doc.find("\n## ", start + 1)
        return doc[:start] + text + ("\n" + doc[end + 1 :] if end != -1 else "")
    heads = [m.start() for m in re.finditer(r"^## ", doc, re.M)]
    at = heads[1] if len(heads) > 1 else len(doc)
    return doc[:at] + text + ("\n" if at < len(doc) else "") + doc[at:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if any section is stale")
    args = parser.parse_args(argv)
    stale = []
    for folder, entries in sorted(collect().items()):
        readme = folder / "README.md"
        if not readme.exists():
            sys.exit(f"{readme.relative_to(ROOT)} is missing; give the folder a README first")
        new = updated(readme, section(folder, entries))
        if new != readme.read_text():
            stale.append(str(readme.relative_to(ROOT)))
            if not args.check:
                readme.write_text(new)
    verb = "stale" if args.check else "updated"
    sys.stdout.write("".join(f"{verb}: {path}\n" for path in stale) or "every section is current\n")
    return 1 if args.check and stale else 0


if __name__ == "__main__":
    sys.exit(main())
