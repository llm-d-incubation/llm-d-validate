"""Compare two runs of the same profile, case by case.

The suite says whether a fleet holds *now*. It cannot say whether the fleet got worse, because
nothing it writes is compared to anything earlier. This module is that comparison: two runs in,
four buckets out, phrased so the result can be pasted into a channel without editing.

What counts as a regression is the whole design, and two of the four decisions are easy to get
wrong:

  pass -> fail        a regression. The thing we are looking for.
  fail -> pass        a recovery, and worth saying: silence after a fix is how people stop
                      trusting the report.
  pass -> skip        NOT a regression. It is lost evidence -- the case stopped running, so the
                      fleet may be fine or may be broken and we no longer know. Reporting it as
                      a failure would train people to ignore the alert; hiding it would let
                      coverage drain away unnoticed. It gets its own line.
  appeared/vanished   the SUITE changed, not the fleet. Never a regression.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..profile.known import FIX_EXPECTED, KNOWN, LABELLED, first_failed_check

# A verdict was reached. Anything else is an absence of evidence, not evidence of health.
_DECIDED = frozenset({"pass", "fail", "error"})
_BAD = frozenset({"fail", "error"})


def key(record: dict) -> tuple[str, str, str]:
    """What makes a case the same case across two runs.

    Ingress is part of it: the same case at the gateway and at the engine are different
    questions, and a stack can regress at one layer while the other is untouched.
    """
    return (
        str(record.get("ingress") or "-"),
        str(record.get("case_id") or record.get("nodeid") or "?"),
        str(record.get("variant") or record.get("surface") or ""),
    )


def label(k: tuple[str, str, str]) -> str:
    ingress, case, variant = k
    return f"{case}[{variant}]@{ingress}" if variant else f"{case}@{ingress}"


@dataclass
class Delta:
    """Everything the two runs disagree about, plus what they agree on."""

    newly_failing: list[str] = field(default_factory=list)
    current_failing: list[str] = field(default_factory=list)
    still_failing: list[str] = field(default_factory=list)
    newly_passing: list[str] = field(default_factory=list)
    still_passing: list[str] = field(default_factory=list)
    stopped_running: list[str] = field(default_factory=list)
    started_passing: list[str] = field(default_factory=list)
    started_failing: list[str] = field(default_factory=list)
    appeared: list[str] = field(default_factory=list)
    vanished: list[str] = field(default_factory=list)
    flaky: list[str] = field(default_factory=list)
    # Newly failing cases that got no HTTP answer at all: a timeout, a dropped connection, a
    # broken stream. Still counted as regressions -- an outage must not be filed under "flaky"
    # -- but named, because a rollout or a flapping replica looks exactly like this and the
    # reader should rerun before opening a finding. An HTTP 5xx is not in this bucket: the
    # fleet answered, and a 500 to a well-formed request is a defect, not a transient.
    transient: list[str] = field(default_factory=list)
    # What the profile's `known_failures` said, as structured entries (`ingress`, `case_id`,
    # `variant`, `label`, `issue`, ...). A failure labelled `known` is reported in its own
    # section, never as a regression: the team knows, and a fix is pending. Four things are
    # said beside it, because a known-failure list that is never pruned or checked is how
    # fixes get lost: a labelled case that passes now (the fix landed: prune the entry); a
    # `fix_expected` case (the awaited build answered and it still fails); a case that passed
    # throughout the window and now fails a declared check (a regression is never known); and
    # a case that was known and now fails a different check (no longer covered).
    known_failing: list[dict] = field(default_factory=list)
    fix_landed: list[dict] = field(default_factory=list)
    fix_missing: list[dict] = field(default_factory=list)
    regressed_known: list[dict] = field(default_factory=list)
    known_changed: list[dict] = field(default_factory=list)
    # New cases moved to known_failing, so the report leaves them out of "failing new cases".
    appeared_known: list[str] = field(default_factory=list)
    window: int = 1
    window_dropped: int = 0
    window_errors: dict[str, str] = field(default_factory=dict)
    digest_changed: bool = False
    profile_diff: list[str] = field(default_factory=list)
    # What the two runs saw behind each ingress (evidence/stack.py), and how it differs. A
    # case that moved with the build is a different question from one that moved on the same
    # build, so this is said before the delta.
    stack_before: dict = field(default_factory=dict)
    stack_after: dict = field(default_factory=dict)
    stack_changes: list[str] = field(default_factory=list)
    mixed_stack: list[str] = field(default_factory=list)
    # The same verdicts in one word per key, for a machine reader such as an alert feed. Only
    # cases the later run decided: `regressed`, `failing`, `recovered`, `passing`, `flaky`,
    # `new` for a case failing with no verdict in the window to compare it to, or `known` for
    # a failure the profile's `known_failures` had filed.
    transitions: dict[tuple[str, str, str], str] = field(default_factory=dict)

    @property
    def regressed(self) -> bool:
        return bool(self.newly_failing)


def compare(before: list[dict], after: list[dict]) -> Delta:
    """Diff two runs. A window of one: any change is reported as a change."""
    return compare_window([before], after)


def compare_window(history: list[list[dict]], after: list[dict]) -> Delta:
    """Diff a run against the runs before it, oldest first.

    A single baseline cannot tell a regression from a flake. We have measured flaky cases --
    an answer-budget case that failed 1 run in 3, throttling that skips nondeterministically --
    and against one baseline each flip reports a regression and the next run reports a recovery.
    A nightly that cries wolf gets muted, and a muted nightly is worse than none.

    So a case is only called newly failing when every verdict in the window agreed it passed.
    A case whose history is mixed is called flaky and reported separately: it is not evidence
    the fleet got worse, and it is not nothing either -- it is a case nobody can currently draw
    a conclusion from, which is its own problem.
    """
    # A verdict means something only against the contract it was produced under. A window that
    # spans a profile change mixes two contracts, and "every run agreed it passed" is then a
    # claim about declarations that no longer apply -- so those runs are dropped from the
    # window rather than counted as agreement.
    current = digests(after)
    usable, dropped = [], 0
    for records in history:
        if not records:
            continue
        theirs = digests(records)
        if current and theirs and theirs != current:
            dropped += 1
            continue
        usable.append(records)
    if not usable and history:
        # Nothing in the window shares the contract. Still worth comparing against the most
        # recent run, but the reader has to be told the contract moved -- which the drift flag
        # below does. It was not "left out" if we went on to use it.
        usable = [records for records in history if records][-1:]
        dropped = 0
    # Two different facts, and conflating them misleads: dropping older runs means the window
    # is shorter than asked for, while this flag means the baseline we are actually comparing
    # against was made under different declarations -- which is what turns findings into
    # questions.
    drift = bool(usable and current and digests(usable[-1]) and digests(usable[-1]) != current)

    runs = [{key(r): r for r in records} for records in usable]
    new = {key(r): r for r in after}
    d = Delta(
        window=len(runs),
        window_dropped=dropped,
        digest_changed=drift,
        current_failing=[label(k) for k in sorted(new) if new[k]["outcome"] in _BAD],
    )
    if not runs:
        return _apply_known(d, new, {})

    previous = runs[-1]
    seen_before = set().union(*(set(run) for run in runs))

    for k in sorted(new.keys() - seen_before):
        d.appeared.append(label(k))
        if new[k]["outcome"] in _DECIDED:
            d.transitions[k] = "new" if new[k]["outcome"] in _BAD else "passing"
    for k in sorted(seen_before - new.keys()):
        d.vanished.append(label(k))

    for k in sorted(seen_before & new.keys()):
        now = new[k]["outcome"]
        name = label(k)
        # Only runs where this case reached a verdict say anything about it.
        verdicts = [run[k]["outcome"] for run in runs if k in run and run[k]["outcome"] in _DECIDED]

        if now not in _DECIDED:
            if k in previous and previous[k]["outcome"] in _DECIDED:
                why = new[k].get("skip_class") or "unclassified"
                d.stopped_running.append(f"{name} ({why})")
            continue
        if not verdicts:
            # Skipped throughout the window and now decided: new evidence, not new breakage.
            (d.started_failing if now in _BAD else d.started_passing).append(name)
            d.transitions[k] = "new" if now in _BAD else "passing"
            continue

        was_bad = {verdict in _BAD for verdict in verdicts}
        if len(was_bad) > 1:
            # The window disagrees with itself, so this run cannot settle anything either.
            d.flaky.append(
                f"{name} ({''.join('F' if v in _BAD else '.' for v in verdicts)}"
                f"{'F' if now in _BAD else '.'})"
            )
            d.transitions[k] = "flaky"
        elif was_bad == {True}:
            (d.still_failing if now in _BAD else d.newly_passing).append(name)
            d.transitions[k] = "failing" if now in _BAD else "recovered"
        else:
            (d.newly_failing if now in _BAD else d.still_passing).append(name)
            d.transitions[k] = "regressed" if now in _BAD else "passing"
            if now in _BAD and transport_failure(new[k]):
                d.transient.append(name)

    return _apply_known(d, new, previous)


def _apply_known(
    d: Delta, new: dict[tuple[str, str, str], dict], previous: dict[tuple[str, str, str], dict]
) -> Delta:
    """Move the failures the profile had already filed out of the regression buckets.

    Only a record labelled `known` whose transition is not `regressed` moves: it leaves
    `newly_failing`, `still_failing`, `started_failing`, `flaky` and `transient` for
    `known_failing`, and its transition becomes `known`. It stays in `current_failing`, which
    counts what fails. A labelled case that passed in every run of the window is a regression
    whatever the profile says, so it stays where it is and is named under `regressed_known`.
    A `fix_expected` record keeps its bucket and is named under `fix_missing`. The previous
    run's label is the authority for the other two: a labelled case that passes now is the
    fix landing, and one that now fails a different check is no longer covered.
    """
    moved: set[str] = set()
    for k in sorted(new):
        record = new[k]
        known = record.get("known_failure") or {}
        status, name = known.get("status"), label(k)
        before = (previous.get(k) or {}).get("known_failure") or {}
        entry = {"ingress": k[0], "case_id": k[1], "variant": k[2] or None, "label": name}
        bad = record["outcome"] in _BAD
        if status == KNOWN and bad:
            if d.transitions.get(k) == "regressed":
                d.regressed_known.append({**entry, "issue": known.get("issue")})
                continue
            d.known_failing.append({**entry, "issue": known.get("issue")})
            moved.add(name)
            if k in d.transitions:
                d.transitions[k] = "known"
        elif status == FIX_EXPECTED and bad:
            d.fix_missing.append(
                {
                    **entry,
                    "issue": known.get("issue"),
                    "observed": dict(known.get("observed") or {}),
                }
            )
        elif bad and before.get("status") in LABELLED:
            # Same check as before: the entry was removed or narrowed. A different check:
            # the case now fails for another reason, which the entry does not speak to.
            d.known_changed.append(
                {
                    **entry,
                    "issue": before.get("issue"),
                    "was_check": before.get("check"),
                    "check": first_failed_check(record.get("checks") or []),
                }
            )
        elif record["outcome"] == "pass" and d.transitions.get(k) == "recovered":
            if before.get("status") in LABELLED:
                d.fix_landed.append({**entry, "issue": before.get("issue")})
    if moved:
        for bucket in ("newly_failing", "still_failing", "started_failing", "transient"):
            setattr(d, bucket, [n for n in getattr(d, bucket) if n not in moved])
        d.flaky = [n for n in d.flaky if n.split(" (")[0] not in moved]
        d.appeared_known = sorted(set(d.appeared) & moved)
    return d


# What a request that never got an HTTP answer records: the exception's class name, which every
# transport_error carries as `{type(exc).__name__}: {exc}`. httpx's timeout and connection
# classes, llmdv's stream idle timeout, and the pinned SDKs' own. Not llmdv's phrases: "stream
# failed" also wraps a malformed 200 stream, which is a defect that recurs on every run.
_TRANSPORT = re.compile(
    r"ReadTimeout|ConnectTimeout|WriteTimeout|PoolTimeout|ConnectError|RemoteProtocolError|"
    r"ReadError|WriteError|StreamIdleTimeout|APITimeoutError|APIConnectionError"
)


def transport_failure(record: dict) -> bool:
    """Whether a failed record got no HTTP answer, as opposed to a wrong one.

    `http_status` is None when no response arrived. The reason text is checked as well because
    a stream can time out after the status line, which leaves a 200 beside a transport error.
    Only exception class names are matched: a malformed 200 stream also fails as "stream
    failed", and that is a defect, not a transient.
    """
    if record.get("http_status") is not None and not str(record.get("http_status")).startswith("2"):
        return False
    return bool(_TRANSPORT.search(str(record.get("reason") or "")))


def digests(records: list[dict]) -> set[str]:
    return {str(r.get("profile_digest")) for r in records if r.get("profile_digest")}


# ------------------------------------------------------------------- the stack behind the runs

# What a run records about each ingress (evidence/stack.py): signal -> value -> responses.
Stack = dict[str, dict[str, dict[str, int]]]
# Which signals mean a different build answered, whatever the layer.
BUILD_SIGNALS = ("system_fingerprint", "x-litellm-version")


def attach_stack(delta: Delta, before: Stack, after: Stack) -> Delta:
    """Record what each run saw behind its ingresses, and what differs."""
    delta.stack_before, delta.stack_after = before or {}, after or {}
    delta.stack_changes = (
        stack_changes(delta.stack_before, delta.stack_after) if before and after else []
    )
    delta.mixed_stack = mixed_builds(delta.stack_after)
    return delta


def mixed_builds(stack: Stack) -> list[str]:
    """Build signals that took more than one value in one run: a rollout in progress."""
    out = []
    for ingress, signals in stack.items():
        for signal in BUILD_SIGNALS:
            values = signals.get(signal) or {}
            if len(values) > 1:
                seen = ", ".join(f"{value} ×{count}" for value, count in values.items())
                out.append(f"{ingress}: {signal} {seen}")
    return out


def stack_changes(before: Stack, after: Stack) -> list[str]:
    """One line per signal whose set of values differs between two runs."""
    out = []
    for ingress in sorted(set(before) | set(after)):
        theirs, ours = before.get(ingress) or {}, after.get(ingress) or {}
        for signal in sorted(set(theirs) | set(ours)):
            was, now = set(theirs.get(signal) or {}), set(ours.get(signal) or {})
            if was != now:
                out.append(f"{ingress}: {signal} {_show(was)} → {_show(now)}")
    return out


def _show(values: set[str]) -> str:
    return ", ".join(sorted(values)) if values else "absent"
