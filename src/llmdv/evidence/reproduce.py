"""reproduce.sh: re-run this exact verification later, or on someone else's machine.

A per-case `repro.sh` replays one HTTP exchange. This replays the *run* -- same profile, same
selectors, same llmdv version -- which is the thing you need when asking "is it still broken?" or
handing a result to whoever owns the layer.

Three things make a run reproducible, and all three are written next to the script rather than
assumed:

    profile.used.yaml   the profile EXACTLY as it was loaded, redacted. A profile in git can
                        change after a run; a verdict is only meaningful against the
                        declarations it was made with.
    env.json            the llmdv, Python and SDK versions. A pinned SDK is part of the claim.
    the setup commands  resolved at run time, with pod names as they were. They go stale, which
                        the script says rather than pretending otherwise.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from .. import __version__


def write(
    run_dir: Path,
    profile_name: str,
    profile_digest: str | None,
    argv: list[str],
    setup_commands: dict[str, list[str]],
    key_envs: list[str],
) -> Path:
    """Write reproduce.sh into the run directory and return its path."""
    lines = [
        "#!/usr/bin/env bash",
        "#",
        f"# Re-run the llmdv verification recorded in this directory ({profile_name}).",
        f"# llmdv {__version__}   profile digest {profile_digest or 'unknown'}",
        "#",
        "# The profile is taken from the archived YAML beside this script, NOT from the",
        "# repository: a profile in git may have changed since, and a verdict only means",
        "# something against the declarations it was made with.",
        "set -euo pipefail",
        'cd "$(dirname "$0")"',
        "",
    ]

    if key_envs:
        lines += ["# Credentials are read from the environment; none is stored here."]
        lines += [f': "${{{name}:?set {name} before running this script}}"' for name in key_envs]
        lines.append("")

    forwards = [cmd for commands in setup_commands.values() for cmd in commands]
    if forwards:
        lines += [
            "# This run was driven through port-forwards. The pod names below were resolved",
            "# when the run happened and will be stale if anything has been rescheduled --",
            "# re-resolve them with:  llmdv profile show profile.used.yaml --setup",
            "#",
        ]
        lines += [f"#   {command}" for command in forwards]
        lines.append("")

    # llmdv options must precede the separator that forwards the remaining argv to pytest.
    boundary = argv.index("--") if "--" in argv else len(argv)
    command = f'{shlex.join(argv[:boundary])} --local-output "$OUT"'
    if boundary < len(argv):
        command += " " + shlex.join(argv[boundary:])

    lines += [
        "# Each invocation creates a fresh run directory under this output root.",
        "# Override the root with LLMDV_RERUN_OUT; earlier runs are preserved.",
        'OUT="${LLMDV_RERUN_OUT:-$(mktemp -d -t llmdv-rerun)}"',
        "",
        "# The original invocation, with the profile repointed at the copy beside this script.",
        command,
        "",
        'echo "rerun results under: $OUT"',
        "",
    ]
    path = run_dir / "reproduce.sh"
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o755)
    return path


def rewrite_argv(argv: list[str], profile_flag_value: str) -> list[str]:
    """The original argv with --profile repointed at the archived copy.

    Strip output locations, including the job parent's private directory reservation.
    """
    out: list[str] = []
    skip_next = False
    tokens = iter(argv)
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            out += [token, *tokens]
            break
        if token == "--profile":
            out += ["--profile", profile_flag_value]
            skip_next = True
            continue
        if token.startswith("--profile="):
            out += ["--profile", profile_flag_value]
            continue
        if token in {"--local-output", "--run-dir"}:
            skip_next = True
            continue
        if token.startswith(("--local-output=", "--run-dir=")):
            continue
        out.append(token)
    return out


def frozen_scope_argv(argv: list[str], name: str) -> list[str]:
    """Use a saved definition instead of consulting a potentially changed built-in set."""
    out = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token == "--test-set":
            skip_next = True
            continue
        if token.startswith("--test-set=") or token in {"--all-test-sets", "--full-suite"}:
            continue
        out.append(token)
    position = out.index("--") if "--" in out else len(out)
    out[position:position] = ["--test-set", name]
    return out
