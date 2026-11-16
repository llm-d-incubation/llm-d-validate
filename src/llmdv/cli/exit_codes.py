"""Exit codes. Separating environment problems from fleet problems is the point.

A connection refused against localhost is almost never a fleet defect -- it is a port-forward
that was never started or has died. Reporting that as a test failure teaches people to
distrust the suite, so it gets its own code and CI can tell the two apart without parsing logs.

`EXITS` is the one table of what each code means. The `--help` epilog, the manual's exit table
and the dashboard all read it; a test keeps the manual in step.
"""

OK = 0
TEST_FAILURES = 1  # the fleet
USAGE = 2  # bad arguments, or an invalid profile
PREFLIGHT = 3  # the vantage point
INTERNAL = 4  # a defect in llmdv itself
# Verification passed, but results or the notification were not delivered. Never returned for a
# failed verification: the verification code always takes precedence.
NOT_DELIVERED = 5
# Stopped by SIGINT or SIGTERM before completion: the run budget, or the pod being stopped.
INTERRUPTED = 6

# Code -> (class, meaning). The class is what outcome.json records as `verification.class`, so it
# describes the verification: 5 means the verification passed, and publication is reported on
# its own.
EXITS = {
    OK: ("ok", "every selected case passed (skips do not fail a run)"),
    TEST_FAILURES: ("fleet", "test failures: the fleet did not behave as the profile declares"),
    USAGE: ("usage", "bad usage, or an invalid profile; nothing was verified"),
    PREFLIGHT: (
        "environment",
        "preflight unsatisfied: your vantage point, not the fleet; nothing was tested",
    ),
    INTERNAL: ("internal", "an internal error: a defect in llmdv itself"),
    NOT_DELIVERED: (
        "ok",
        "verification passed, but results or the notification were not delivered",
    ),
    INTERRUPTED: (
        "interrupted",
        "stopped by SIGINT or SIGTERM, e.g. a run budget or a pod being stopped; "
        "what finished is kept and published",
    ),
}
CLASS = {code: name for code, (name, _) in EXITS.items()}
MEANING = {code: meaning for code, (_, meaning) in EXITS.items()}
# The class of a code llmdv never returns itself.
CRASHED = "crashed"


def class_of(code: int | None) -> str:
    """The class for an exit code. Anything llmdv never returns itself -- 137 after an OOM kill,
    say -- means the process died before it could record how the run ended."""
    return CLASS.get(code, CRASHED) if code is not None else CRASHED


def epilog() -> str:
    """The exit-code table for `llmdv --help`."""
    return "exit codes:\n" + "\n".join(f"  {code}  {meaning}" for code, meaning in MEANING.items())
