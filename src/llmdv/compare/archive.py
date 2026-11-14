"""Read finished runs back out of the place `--remote-output` put them.

The sinks are write-only by design: a run publishes and forgets. Comparison needs the opposite,
so this adds the two reads it takes -- which runs exist for a profile, and what one of them
found -- without giving the sinks a general read surface nobody else wants.

Layout is the one `publish` writes: `<prefix>/<profile>/<run-id>/results.jsonl`. Run ids begin
with a UTC timestamp, so lexical order is chronological and "the previous run" is a sort away.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

from ..output import manifest
from ..output.remote import UnsupportedRemote, parse_remote_uri


class NoSuchRun(Exception):
    pass


class InvalidRun(Exception):
    """Results cannot support a comparison because a record is malformed."""


class UnsafeName(Exception):
    """A profile or run id that would address something outside the archive."""


def safe(value: str, what: str) -> str:
    """Reject a path segment that could leave the prefix it belongs under.

    A profile name comes from a field somebody writes in a YAML file, and a run id from a
    recorded result. Both become path segments here. On a shared bucket, `../other-team` would
    otherwise read another team's runs -- and `publish_one` would write into them.
    """
    text = str(value)
    if not text or text in {".", ".."} or "/" in text or "\\" in text or text.startswith("."):
        raise UnsafeName(f"unsafe {what}: {value!r}")
    return text


def _s3(target, patience=None):
    """A sink for reading. Advisory by default: a comparison is worth a few seconds, not a
    nightly's patience, and the job must not sit on a flapping store to enrich a report."""
    from ..output.remote import GLANCE
    from ..output.s3 import S3Sink

    return S3Sink(target, patience or GLANCE)


def list_runs(uri: str, profile: str) -> list[str]:
    """Completely published run ids for one profile, oldest first."""
    profile = safe(profile, "profile name")
    target = parse_remote_uri(uri)
    if target.scheme == "file":
        base = Path(target.location) / profile
        if not base.is_dir():
            return []
        return sorted(
            d.name
            for d in base.iterdir()
            if (d / "results.jsonl").is_file() and (d / manifest.FILENAME).is_file()
        )
    if target.scheme == "s3":
        # Results may arrive before another upload fails. Only the manifest, published last,
        # marks a complete publication; require both files under the same immediate run id.
        keys = set(_s3(target).list_keys(profile))
        names = {rest.rsplit("/", 1)[0] for rest in keys if rest.endswith("/results.jsonl")}
        return sorted(n for n in names if n and "/" not in n and f"{n}/{manifest.FILENAME}" in keys)
    raise UnsupportedRemote(f"cannot read runs from {target.scheme}://")


def read_results(uri: str, profile: str, run_id: str) -> list[dict]:
    """One run's results.jsonl, as records."""
    profile = safe(profile, "profile name")
    run_id = safe(run_id, "run id")
    target = parse_remote_uri(uri)
    if target.scheme == "file":
        path = Path(target.location) / profile / run_id / "results.jsonl"
        if not path.is_file():
            raise NoSuchRun(f"no results.jsonl for {profile}/{run_id} under {uri}")
        text = path.read_text(encoding="utf-8")
    elif target.scheme == "s3":
        sink = _s3(target)
        try:
            text = sink.get_bytes(profile, run_id, "results.jsonl").decode("utf-8")
        except Exception as exc:  # the store's own not-found, whichever flavour it is
            raise NoSuchRun(f"cannot read {profile}/{run_id}/results.jsonl: {exc}") from exc
    else:
        raise UnsupportedRemote(f"cannot read runs from {target.scheme}://")
    return _records(text, run_id)


def read_local_results(run_dir: Path) -> list[dict]:
    """Read the finishing run strictly, even before its publication has completed."""
    path = run_dir / "results.jsonl"
    return _records(path.read_text(encoding="utf-8"), run_dir.name) if path.is_file() else []


def _records(text: str, run_id: str) -> list[dict]:
    """Reject partial or malformed input instead of comparing a silently reduced set."""
    records = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        where = f"run {run_id}, line {line_number}"
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise InvalidRun(f"{where}: invalid JSON") from exc
        if not isinstance(record, dict) or record.get("outcome") not in (
            "pass",
            "fail",
            "skip",
            "error",
        ):
            raise InvalidRun(f"{where}: expected a result with outcome pass, fail, skip or error")
        records.append(record)
    return records


class EmptyRun(Exception):
    """A run that recorded no results. Not a comparison, and never a clean one."""


def require_records(records: list[dict], run_id: str) -> list[dict]:
    """Zero cases is not a pass.

    A run can record nothing for reasons that have nothing to do with the fleet: an OOM kill, a
    selector that matched no cases, a preflight failure. Compared against a healthy run every
    case simply vanishes, and without this guard that renders as "unchanged, nothing failing"
    with a clean exit -- a green verdict over an empty set, which is the worst thing this tool
    could say. `banner.finish` refuses the same shape for the same reason.
    """
    if not records:
        raise EmptyRun(f"run {run_id} recorded no results; there is nothing to compare")
    return records


def pick_baseline(runs: list[str], exclude: str | None = None, baseline: str | None = None) -> str:
    """The archive run to compare a just-finished run against.

    `exclude` is the finishing run's own id: if it is already in the archive -- a rerun, or a
    comparison made after publishing -- comparing it to itself would report a clean diff and
    hide whatever actually changed.
    """
    candidates = [run for run in runs if run != exclude]
    if baseline:
        if baseline not in candidates:
            raise NoSuchRun(f"baseline run {baseline!r} not found in the archive")
        return baseline
    if not candidates:
        raise NoSuchRun("the archive holds no earlier run of this profile")
    return candidates[-1]


def pick_window(
    runs: list[str], exclude: str | None = None, baseline: str | None = None, size: int = 1
) -> list[str]:
    """The runs to judge a finishing run against, oldest first.

    One run by default: the comparison answers "what changed since last time", and the most
    recent run is what "last time" means. A larger window trades that directness for tolerance
    of a flaky case, and is worth setting once a profile has a flake worth tolerating.

    An explicit baseline pins a single comparison point and ignores the window: when somebody
    names the run from before a rollout, they mean that run, not a consensus around it.
    """
    candidates = [run for run in runs if run != exclude]
    if baseline:
        if baseline not in candidates:
            raise NoSuchRun(f"baseline run {baseline!r} not found in the archive")
        return [baseline]
    if not candidates:
        raise NoSuchRun("the archive holds no earlier run of this profile")
    return candidates[-max(1, size) :]


def pick_pair(runs: list[str], baseline: str | None = None) -> tuple[str, str]:
    """The two runs to compare: a named baseline and the latest, or the latest two.

    Refuses rather than guesses when there is only one run. A comparison against nothing is not
    a clean bill of health, and reporting it as one is how a first run gets mistaken for a pass.
    """
    if not runs:
        raise NoSuchRun("no runs found for that profile")
    if baseline:
        if baseline not in runs:
            raise NoSuchRun(f"baseline run {baseline!r} not found")
        if baseline == runs[-1]:
            raise NoSuchRun(f"baseline {baseline!r} is the latest run; nothing to compare it to")
        return baseline, runs[-1]
    if len(runs) < 2:
        raise NoSuchRun(f"only one run exists ({runs[0]}); nothing to compare it to")
    return runs[-2], runs[-1]


PROFILE_FILE = "profile.used.yaml"
NIGHTLY_TRIGGER = "nightly"


def read_outcome(uri: str, profile: str, run_id: str) -> dict | None:
    """A run's outcome.json as it was published, or None when it cannot be read."""
    profile = safe(profile, "profile name")
    run_id = safe(run_id, "run id")
    target = parse_remote_uri(uri)
    try:
        if target.scheme == "file":
            path = Path(target.location) / profile / run_id / "outcome.json"
            text = path.read_text(encoding="utf-8") if path.is_file() else None
        elif target.scheme == "s3":
            text = _s3(target).get_bytes(profile, run_id, "outcome.json").decode("utf-8")
        else:
            return None
        body = json.loads(text) if text is not None else None
        return body if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001 -- advisory; a missing outcome is not an error here
        return None


def read_trigger(uri: str, profile: str, run_id: str) -> str | None:
    """Read a run's trigger label, or None when its outcome cannot identify one."""
    labels = (read_outcome(uri, profile, run_id) or {}).get("labels", {})
    return labels.get("trigger") if isinstance(labels, dict) else None


def read_stack(uri: str, profile: str, run_id: str) -> dict:
    """The stack block a published run recorded, or an empty one for runs that predate it."""
    stack = (read_outcome(uri, profile, run_id) or {}).get("stack")
    return stack if isinstance(stack, dict) else {}


def read_profile(uri: str, profile: str, run_id: str) -> str | None:
    """The profile a run was driven with, as it was published beside the results.

    Returns None rather than raising: knowing a profile changed is already useful, and failing
    to fetch the old one must not cost the comparison.
    """
    profile = safe(profile, "profile name")
    run_id = safe(run_id, "run id")
    target = parse_remote_uri(uri)
    try:
        if target.scheme == "file":
            path = Path(target.location) / profile / run_id / PROFILE_FILE
            return path.read_text(encoding="utf-8") if path.is_file() else None
        if target.scheme == "s3":
            return _s3(target).get_bytes(profile, run_id, PROFILE_FILE).decode("utf-8")
    except Exception:  # noqa: BLE001 -- advisory; the drift warning stands without it
        return None
    return None


def declaration_diff(before: str | None, after: str | None, cap: int = 12) -> list[str]:
    """What actually changed between two profiles, comments and blank lines ignored.

    "The profile changed" leaves a reader to find the difference themselves, and a single
    tightened declaration can explain an entire delta -- adding one surface to a profile
    explained every case that moved, in one line.
    """
    if not before or not after:
        return []

    def meaningful(text: str) -> list[str]:
        return [
            line.rstrip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    changed = [
        line
        for line in difflib.unified_diff(meaningful(before), meaningful(after), lineterm="", n=0)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    return changed[:cap] + ([f"… and {len(changed) - cap} more"] if len(changed) > cap else [])


def publish_one(uri: str, profile: str, run_id: str, path: Path) -> str:
    """Upload a single file into a run that has already been published.

    `llmdv job` hands `--remote-output` to the verify child, so the run uploads itself inside that
    process and `publish_run` afterwards is only a fallback. Anything the job writes once verify
    has returned -- the comparison, for instance -- therefore misses the upload entirely and
    lives only in a pod's temporary directory. This puts that one file where the rest of the run
    already is. Raises; the caller decides whether losing it matters.
    """
    profile = safe(profile, "profile name")
    run_id = safe(run_id, "run id")
    target = parse_remote_uri(uri)
    if target.scheme == "file":
        destination = Path(target.location) / profile / run_id / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
        return str(destination)
    if target.scheme == "s3":
        return _s3(target).put_bytes(path.read_bytes(), profile, run_id, path.name)
    raise UnsupportedRemote(f"cannot publish to {target.scheme}://")
