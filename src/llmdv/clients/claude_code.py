"""Pinned CLI invocation settings; never install or upgrade a client implicitly."""

from __future__ import annotations

import os
from pathlib import Path

from ..profile.schema import ClientBinary
from .process import ProcessResult, run_process


def isolated_env(directory: Path, base_url: str, model: str) -> dict[str, str]:
    # HOME here defines the child process's actual disposable home; the parent is unchanged.
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(directory),
        "TMPDIR": str(directory),
        "XDG_CONFIG_HOME": str(directory / "config"),
        "CLAUDE_CONFIG_DIR": str(directory / "claude"),
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": "not-required",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "256",
        "MAX_THINKING_TOKENS": "0",
    }


def invoke(
    binary: ClientBinary,
    *,
    directory: Path,
    env: dict[str, str],
    prompt: str,
    timeout: float,
    session: str | None = None,
    resume: bool = False,
) -> ProcessResult:
    args = [
        str(binary.executable),
        "--print",
        "--output-format",
        "json",
        "--model",
        env["ANTHROPIC_MODEL"],
        "--tools",
        "",
        "--disallowedTools",
        "*",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--setting-sources",
        "",
        "--settings",
        '{"disableAllHooks":true}',
        "--disable-slash-commands",
        "--max-turns",
        "1",
    ]
    if session:
        args.extend(["--resume" if resume else "--session-id", session])
    else:
        args.append("--no-session-persistence")
    args.append(prompt)
    return run_process(args, cwd=directory, env=env, timeout=timeout)


# A session works through tools over many turns, so its bounds are wider than the probe's.
SESSION_OUTPUT_TOKENS = 8192
SESSION_GENERATIONS = 120
SESSION_BODY_BYTES = 4 * 1024 * 1024
SESSION_STDOUT_BYTES = 8 * 1024 * 1024
NO_MCP = '{"mcpServers":{}}'
FILE_WRITERS = ("Edit", "Write")
# One rule covers every file-writing tool, relative to the directory the session runs in.
PROJECT_WRITES = "Edit(./**)"
# `.claude/settings.json` in the project defines HOOKS, which are shell commands, and the
# session is started with `--setting-sources project` so that file is read. A session that
# could rewrite it could grant itself a command. Whether `./**` matches a dotfile is a property
# of the matcher rather than of this contract, so the denial is explicit: a deny rule wins over
# an allow rule, and the boundary no longer depends on glob semantics.
PROJECT_SETTINGS = "Edit(./.claude/**)"


def session_env(
    directory: Path, base_url: str, model: str, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """The isolated environment, with room to think and answer at session length."""
    env = isolated_env(directory, base_url, model)
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(SESSION_OUTPUT_TOKENS)
    # A session is allowed to think; the probe is not. pop rather than del, so this does not
    # become a KeyError the day isolated_env stops setting it.
    env.pop("MAX_THINKING_TOKENS", None)
    env.update(extra or {})
    return env


def invoke_session(
    binary: ClientBinary,
    *,
    project: Path,
    env: dict[str, str],
    prompt: str,
    timeout: float,
    allowed_tools: tuple[str, ...],
    restrict_tools: bool = True,
    max_turns: int = 40,
) -> ProcessResult:
    """Run one headless session in `project`, with only the listed tools and project settings.

    `--tools` removes every other built-in tool; `--allowedTools` then grants the listed ones,
    patterns included, without skipping permission checks. `restrict_tools=False` leaves the
    full tool set defined and relies on permissions alone. The event stream is requested so a
    case can see which tools the model used.

    File writes are granted for the project directory only, except `.claude/`, which holds the
    settings and hooks this session is started with. Reads are not confined, and a session
    allowed to run a command runs whatever the model wrote, so this is not a sandbox.
    """
    # "Bash(python3 -m unittest)" is a permission pattern on the Bash tool.
    available = list(dict.fromkeys(tool.split("(", 1)[0] for tool in allowed_tools))
    granted = [tool for tool in allowed_tools if tool not in FILE_WRITERS]
    if len(granted) != len(allowed_tools):
        granted.append(PROJECT_WRITES)
    args = [
        str(binary.executable),
        "--print",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        env["ANTHROPIC_MODEL"],
        "--max-turns",
        str(max_turns),
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config",
        NO_MCP,
        "--setting-sources",
        "project",
        *(("--tools", *available) if restrict_tools else ()),
        "--disallowedTools",
        PROJECT_SETTINGS,
        # Variadic, so it goes last.
        "--allowedTools",
        *granted,
    ]
    return run_process(args, cwd=project, env=env, timeout=timeout, cap=SESSION_STDOUT_BYTES)
