"""Exit codes. Separating environment problems from fleet problems is the point.

A connection refused against localhost is almost never a fleet defect -- it is a port-forward
that was never started or has died. Reporting that as a test failure teaches people to
distrust the suite, so it gets its own code and CI can tell the two apart without parsing logs.
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

CLASS = {
    OK: "ok",
    TEST_FAILURES: "fleet",
    USAGE: "usage",
    PREFLIGHT: "environment",
    INTERNAL: "internal",
    INTERRUPTED: "interrupted",
}


def class_of(code: int | None) -> str:
    """The class for an exit code. Anything llmdv never returns itself -- 137 after an OOM kill,
    say -- means the process died before it could record how the run ended."""
    return CLASS.get(code, "crashed") if code is not None else "crashed"
