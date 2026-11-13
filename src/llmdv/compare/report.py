"""Render a Delta as text that reads the same in a terminal and in a Slack message.

Slack's mrkdwn and plain text agree on `*bold*` and backticks, so one rendering serves both and
there is no second format to keep in step. The verdict goes first: somebody scanning a channel
reads one line, and that line has to be enough to decide whether to open anything.
"""

from __future__ import annotations

from .diff import Delta

CAP = 10

# Written into the run directory, so the analysis is published with the run rather than
# existing only in a chat message somebody scrolls past.
COMPARISON_FILE = "comparison.md"
# The same comparison for a machine reader, such as the alert feed `llmdv job` writes.
COMPARISON_JSON = "comparison.json"
COMPARISON_SCHEMA = "llmdv.comparison/1"


def as_record(delta: Delta, before_id: str, after_id: str) -> dict:
    """The comparison as data: which run it was made against, and each decided case's verdict."""
    return {
        "schema": COMPARISON_SCHEMA,
        "made": True,
        "baseline": before_id,
        "run": after_id,
        "window": delta.window,
        "window_dropped": delta.window_dropped,
        "profile_changed": delta.digest_changed,
        "stack": {
            "before": delta.stack_before,
            "after": delta.stack_after,
            "changes": list(delta.stack_changes),
            "mixed": list(delta.mixed_stack),
        },
        "regressed": delta.regressed,
        "transient": list(delta.transient),
        "known": {
            "failing": list(delta.known_failing),
            "fix_landed": list(delta.fix_landed),
            "fix_missing": list(delta.fix_missing),
            "regressed": list(delta.regressed_known),
            "changed": list(delta.known_changed),
        },
        "transitions": [
            {"ingress": ingress, "case_id": case, "variant": variant, "transition": transition}
            for (ingress, case, variant), transition in sorted(delta.transitions.items())
        ],
    }


def not_made(reason: str) -> dict:
    """A comparison that could not be made. Not the same as one that found nothing."""
    return {"schema": COMPARISON_SCHEMA, "made": False, "reason": reason}


def _bullets(names: list[str], cap: int = CAP) -> list[str]:
    out = [f"  • `{n}`" for n in names[:cap]]
    if len(names) > cap:
        out.append(f"  • … and {len(names) - cap} more")
    return out


def render(delta: Delta, profile: str, before_id: str, after_id: str) -> str:
    """One message: the verdict, then only the sections that have something in them."""
    if delta.regressed:
        headline = f"*{profile}* — {len(delta.newly_failing)} newly failing"
    elif delta.current_failing:
        known = f", {len(delta.known_failing)} of them known" if delta.known_failing else ""
        headline = f"*{profile}* — {len(delta.current_failing)} failing{known}, no new regressions"
    elif delta.newly_passing:
        headline = f"*{profile}* — no new failures, {len(delta.newly_passing)} newly passing"
    elif delta.flaky:
        headline = f"*{profile}* — no new failures, {len(delta.flaky)} flaky"
    else:
        headline = f"*{profile}* — unchanged, nothing failing"

    lines = [headline, f"`{before_id}` → `{after_id}`"]
    if delta.window > 1:
        lines.append(f"_compared against the last {delta.window} runs_")
    if delta.window_dropped:
        lines.append(f"_{delta.window_dropped} earlier run(s) left out of the window_")
        if changed := delta.window_dropped - len(delta.window_errors):
            lines.append(
                f"_{changed} made under a different profile, so their verdicts do not speak "
                "to this contract_"
            )
        lines += [f"  • `{run_id}`: {reason}" for run_id, reason in delta.window_errors.items()]
    lines.append("")

    # Said before the delta: a case that moved with the build is a different question from one
    # that moved on the same build, and a run that saw two builds straddled a rollout.
    if delta.stack_changes:
        lines.append("*The stack changed between these runs.* A case below may have moved with it:")
        lines += [f"  `{change}`" for change in delta.stack_changes]
        lines.append("")
    if delta.mixed_stack:
        lines.append(
            "_This run saw more than one build behind an ingress, so it straddled a rollout; "
            "its verdicts mix two fleets:_"
        )
        lines += [f"  `{line}`" for line in delta.mixed_stack]
        lines.append("")

    if delta.digest_changed:
        lines += [
            "*The profile changed between these runs.* Declarations differ, so a case can move "
            "because the contract moved rather than because the fleet did. Treat the lines "
            "below as questions, not findings.",
            "",
        ]
        if delta.profile_diff:
            lines.append("What changed in the profile:")
            lines += [f"  `{line}`" for line in delta.profile_diff]
            lines.append("")

    if delta.newly_failing:
        lines.append(f"*Newly failing as of* `{after_id}` ({len(delta.newly_failing)})")
        lines += _bullets(delta.newly_failing)
        if delta.regressed_known:
            lines.append(
                f"  _{len(delta.regressed_known)} of these match a `known_failures` entry but "
                "passed in every run of the window. A regression is never known: either the "
                "fleet re-broke after the fix, or the entry excuses a failure. Check the entry:_"
            )
            lines += [f"    `{e['label']}` ({e['issue']})" for e in delta.regressed_known[:CAP]]
        if delta.transient:
            # Named, not removed: a rollout or a flapping replica produces exactly these, and
            # so does an outage. The reader reruns before deciding which.
            lines.append(
                f"  _{len(delta.transient)} of these got no HTTP answer (a timeout or a dropped "
                "connection), which a rollout or a flapping replica also produces; rerun them "
                "before opening a finding:_"
            )
            lines += [f"    `{n}`" for n in delta.transient[:CAP]]
        lines.append("")
    if delta.still_failing:
        lines.append(f"*Existed failing* ({len(delta.still_failing)})")
        lines += _bullets(delta.still_failing)
        lines.append("")
    if delta.newly_passing:
        lines.append(f"*Newly passing as of* `{after_id}` ({len(delta.newly_passing)})")
        lines += _bullets(delta.newly_passing)
        lines.append("")
    if delta.fix_landed:
        lines.append(
            f"*Fix landed* ({len(delta.fix_landed)}) — known failures now passing; remove "
            "their `known_failures` entries from the profile"
        )
        lines += _bullets([f"{e['label']} ({e['issue']})" for e in delta.fix_landed])
        lines.append("")
    if delta.fix_missing:
        lines.append(
            f"*Fix expected, still failing* ({len(delta.fix_missing)}) — the build the "
            "entry waited for answered, and the case still fails. Counted above as failures."
        )
        lines += _bullets(
            [
                f"{e['label']} ({e['issue']}; saw "
                + ", ".join(f"{k}={v}" for k, v in (e.get("observed") or {}).items())
                + ")"
                for e in delta.fix_missing
            ]
        )
        lines.append("")
    if delta.known_changed:
        lines.append(
            f"*No longer covered* ({len(delta.known_changed)}) — was a known failure in the "
            "previous run and is a plain failure now: the entry was removed, or the case fails "
            "a different check. Counted above as failures."
        )
        lines += _bullets([_changed(e) for e in delta.known_changed])
        lines.append("")
    if delta.known_failing:
        lines.append(
            f"*Known failures, fix pending* ({len(delta.known_failing)}) — filed and declared "
            "in the profile; not regressions"
        )
        lines += _bullets([f"{e['label']} ({e['issue']})" for e in delta.known_failing])
        lines.append("")

    lines.append(f"*Existing passing*: {len(delta.still_passing)}")

    # Cases the window disagrees about. Not regressions -- nobody can draw a conclusion from
    # them, which is a different problem and wants a different fix: stabilise the case, or
    # declare what the fleet actually does.
    if delta.flaky:
        lines += [
            "",
            f"*Flaky across the window* ({len(delta.flaky)}) — not reported as regressions; "
            "the runs disagree. `.` passed, `F` failed, oldest first",
        ]
        lines += _bullets(delta.flaky)

    # Lost and regained evidence. Not failures, and not nothing: a case that stopped running
    # means the fleet may be fine or may be broken and nobody would know.
    if delta.stopped_running:
        lines += [
            "",
            f"*Stopped running* ({len(delta.stopped_running)}) — not failures, but no longer "
            "evidence of anything",
        ]
        lines += _bullets(delta.stopped_running)
    if delta.started_failing:
        lines += [
            "",
            f"*Now running and failing* ({len(delta.started_failing)}) — not a regression, "
            "these were skipped before. New evidence, not new breakage.",
        ]
        lines += _bullets(delta.started_failing)
    if delta.started_passing:
        lines.append(f"*Started running and passing*: {len(delta.started_passing)}")

    failing_new_cases = sorted(
        set(delta.appeared) & set(delta.current_failing) - set(delta.appeared_known)
    )
    if failing_new_cases:
        lines += [
            "",
            f"*Failing new cases* ({len(failing_new_cases)}) — not regressions; "
            "these cases were absent from the comparison window.",
        ]
        lines += _bullets(failing_new_cases)

    # Suite churn, kept separate so it is never read as a fleet change.
    churn = []
    if delta.appeared:
        churn.append(f"{len(delta.appeared)} new case(s)")
    if delta.vanished:
        churn.append(f"{len(delta.vanished)} case(s) removed")
    if churn:
        lines += ["", f"_Suite changed: {', '.join(churn)}._"]
        # Only say whose doing it is when the shape supports it. A run that lost most of its
        # cases looks identical to a suite that shrank, and a crash is the likelier cause.
        lost = len(delta.vanished)
        kept = len(delta.still_passing) + len(delta.still_failing) + len(delta.newly_failing)
        if lost and kept and lost > kept:
            lines.append(
                f"_{lost} case(s) disappeared against {kept} still reporting. A crashed or "
                "mis-selected run looks exactly like this -- check the later run ended "
                "normally before reading the rest._"
            )
        elif not delta.vanished:
            lines.append("_That is us, not the fleet._")

    return "\n".join(lines)


def _changed(entry: dict) -> str:
    was, now = entry.get("was_check"), entry.get("check")
    if now and was and now != was:
        return f"{entry['label']} (was {entry['issue']} for `{was}`; now fails `{now}`)"
    return f"{entry['label']} (was {entry['issue']}; same check `{now}`, no entry covers it now)"
