"""Report how llm-d has moved since the commit the guide profiles were built from.

Reads a local llm-d clone and this repository's `profiles/llm-d/`, and writes a Markdown report
on stdout:

- **Lanes with no profile**: a nightly lane at the newer commit that no guide folder's README
  names. A lane of a guide with no folder here is a new Tier 3 guide.
- **Lanes removed**: a lane a guide folder's README names that no longer exists.
- **Changed sources, by guide**: the files under each covered guide's directory, its nightly
  workflows and the shared recipes that changed between the two commits, grouped by the
  profile fact each kind of file owns.

Read-only: it runs `git` against the clone and never writes to it, to this repository or to any
cluster. It reports file paths and lane names only, never the values a workflow passes
(namespaces, cluster names, buckets), which must not be copied into this repository.

    python .agents/skills/tracking-llm-d-guide-drift/drift.py --llm-d <clone> [--to origin/main]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
GUIDES = REPO / "profiles" / "llm-d"
WORKFLOWS = ".github/workflows"
LANE_PREFIX = "nightly-e2e-"
# The commit the profiles were built from, as profiles/llm-d/README.md states it.
PIN = re.compile(r"at llm-d `([0-9a-f]{7,40})`")
UPSTREAM_DIR = re.compile(r"https://github\.com/llm-d/llm-d/tree/[^/]+/(guides/[^)\s#]+)")
# Which profile fact each kind of upstream file owns (the profile skill's source table).
OWNERS = (
    (re.compile(rf"^{WORKFLOWS}/"), "lanes: which configurations get a profile"),
    (re.compile(r"/scripts/|deploy.*\.sh$"), "lanes: the deploy scripts a lane calls"),
    (re.compile(r"(^|/)guide\.yaml$"), "statements to check against the lanes (Upstream notes)"),
    (
        re.compile(r"(^|/)README\.md$|\.md$"),
        "statements to check against the lanes (Upstream notes)",
    ),
    (
        re.compile(r"router|values\.ya?ml$|epp"),
        "router configuration: plugins, flow control, headers",
    ),
    (
        re.compile(r"modelserver|kustomization\.ya?ml$|\.ya?ml$"),
        "model-server overlays: model, window, parsers, engine build and flags",
    ),
)
OTHER = "other files"


@dataclass
class Guide:
    folder: str
    upstream: str
    known_lanes: set[str] = field(default_factory=set)


def git(clone: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), *args], check=True, capture_output=True, text=True
    ).stdout


def pinned_commit() -> str:
    match = PIN.search((GUIDES / "README.md").read_text())
    if not match:
        sys.exit("profiles/llm-d/README.md names no `at llm-d <commit>`; pass --from")
    return match.group(1)


def guides() -> list[Guide]:
    found = []
    for readme in sorted(GUIDES.glob("*/README.md")):
        folder = readme.parent.name
        if folder == "parts":
            continue
        text = readme.read_text()
        upstream = UPSTREAM_DIR.search(text)
        lanes = set(re.findall(r"`([a-z0-9][a-z0-9-]*)`", text))
        found.append(Guide(folder, upstream.group(1) if upstream else f"guides/{folder}", lanes))
    return found


def lanes_at(clone: Path, ref: str) -> set[str]:
    names = git(clone, "ls-tree", "--name-only", f"{ref}:{WORKFLOWS}").split()
    return {Path(n).stem.removeprefix(LANE_PREFIX) for n in names if n.startswith(LANE_PREFIX)}


def owner(guides_: list[Guide], lane: str) -> Guide | None:
    """The guide folder a lane belongs to: the longest folder name the lane starts with or holds."""
    matches = [g for g in guides_ if lane.startswith(g.folder + "-") or f"-{g.folder}-" in lane]
    return max(matches, key=lambda g: len(g.folder), default=None)


def covered(guide: Guide, lane: str) -> bool:
    """A lane is covered when the guide's README names it, in full or as its suffix."""
    suffix = lane.split(guide.folder + "-", 1)[-1]
    return bool({lane, LANE_PREFIX + lane, suffix} & guide.known_lanes)


def kind_of(path: str) -> str:
    return next((label for pattern, label in OWNERS if pattern.search(path)), OTHER)


def changed(clone: Path, old: str, new: str, *paths: str) -> list[tuple[str, str]]:
    out = git(clone, "diff", "--name-status", "--no-renames", old, new, "--", *paths)
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if line]


def report(clone: Path, old: str, new: str) -> str:
    old_sha, new_sha = (git(clone, "rev-parse", "--short", r).strip() for r in (old, new))
    guides_ = guides()
    before, after = lanes_at(clone, old), lanes_at(clone, new)
    out = [f"# llm-d drift: `{old_sha}` → `{new_sha}`", ""]

    uncovered: dict[str, list[str]] = {}
    for lane in sorted(after):
        guide = owner(guides_, lane)
        if guide is None or not covered(guide, lane):
            uncovered.setdefault(guide.folder if guide else "(no guide folder)", []).append(lane)
    out += ["## Lanes with no profile", ""]
    if not uncovered:
        out.append("None.")
    for folder, lanes in uncovered.items():
        out.append(f"- **{folder}**: " + ", ".join(f"`{LANE_PREFIX}{lane}`" for lane in lanes))
    out.append("")

    removed = sorted(before - after)
    out += ["## Lanes removed", ""]
    out += [f"- `{LANE_PREFIX}{lane}`" for lane in removed] or ["None."]
    out.append("")

    out += ["## Changed sources, by guide", ""]
    shared = changed(clone, old, new, "guides/recipes", "guides/env.sh")
    for guide in guides_:
        workflows = [
            f"{WORKFLOWS}/{LANE_PREFIX}{lane}.yaml"
            for lane in sorted(before | after)
            if owner(guides_, lane) is guide
        ]
        files = changed(clone, old, new, guide.upstream, *workflows)
        if not files:
            continue
        out += [f"### {guide.folder} (`{guide.upstream}`)", ""]
        by_kind: dict[str, list[str]] = {}
        for status, path in files:
            by_kind.setdefault(kind_of(path), []).append(f"`{status}` {path}")
        for kind, paths in by_kind.items():
            out.append(f"- **{kind}**")
            out += [f"  - {p}" for p in paths]
        out.append("")
    if shared:
        out += ["### Shared by every guide (`guides/recipes`, `guides/env.sh`)", ""]
        out += [f"- `{status}` {path}" for status, path in shared]
        out.append("")
    out.append(f"Diff any file with `git -C <clone> diff {old_sha} {new_sha} -- <path>`.")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--llm-d", required=True, type=Path, help="a local clone of llm-d")
    parser.add_argument("--from", dest="old", help="the commit the profiles were built from")
    parser.add_argument("--to", dest="new", default="origin/main", help="default origin/main")
    args = parser.parse_args(argv)
    sys.stdout.write(report(args.llm_d, args.old or pinned_commit(), args.new))
    return 0


if __name__ == "__main__":
    sys.exit(main())
