"""Bound a trusted, operator-supplied client process without a shell or inherited secrets."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..http.redact import scrub

# How long a leader that refused the group kill may take to finish exiting. Measured on macOS
# under load, an exiting leader became reapable within 0.1 s; this only bounds a real failure.
EXIT_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class ProcessResult:
    code: int
    stdout: str
    stderr: str
    problem: str | None = None


def run_process(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float, cap: int = 262144
) -> ProcessResult:
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    problem = None
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )  # noqa: S603 -- explicit operator-pinned executable
    try:
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                stream = getattr(process, name)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    problem = "client timeout"
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    data = os.read(key.fileobj.fileno(), 8192)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = buffers[key.data]
                    remaining_bytes = cap - len(buffer)
                    buffer.extend(data[:remaining_bytes])
                    if len(data) > remaining_bytes:
                        problem = "client output exceeded evidence cap"
                        break
                if problem:
                    break
            if problem is None:
                try:
                    process.wait(timeout=max(0.01, timeout - (time.monotonic() - started)))
                except subprocess.TimeoutExpired:
                    problem = "client timeout"
    finally:
        # Kill descendants too, including ones that inherited pipes after the parent exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS answers EPERM, not ESRCH, when no member of the group can take a signal:
            # all are unreaped zombies or part-way through exiting, because the leader exited
            # before we got here. There is nothing left to kill, and raising would replace the
            # verdict (or the exception in flight) and skip the reap. A leader caught mid-exit
            # is not reapable yet, so allow it a moment; one still running after that means
            # the kill genuinely failed.
            try:
                process.wait(timeout=EXIT_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                still_running = True
            else:
                still_running = False
            if still_running:
                raise
        process.wait()
        process.stdout.close()
        process.stderr.close()
    return ProcessResult(
        process.returncode,
        scrub(buffers["stdout"].decode("utf-8", "replace")),
        scrub(buffers["stderr"].decode("utf-8", "replace")),
        problem,
    )
