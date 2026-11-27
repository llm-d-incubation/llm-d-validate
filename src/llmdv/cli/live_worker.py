"""Internal pytest session for an exact assignment. Only the coordinator finalizes the run."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import signal
import socket
import sqlite3
import sys
import threading
from functools import partial
from pathlib import Path

import pytest

from ..clients import process
from ..evidence import journal
from ..http import redact
from ..log import setup
from ..output.local import RUN_STATE
from . import style
from .verify_cmd import _run_pytest


def _label_case(record: logging.LogRecord, *, nodes: dict[str, str]) -> bool:
    """Keep interleaved progress and diagnostics attributable to the public case ID."""
    case = journal.current()
    if case is not None and not getattr(record, "llmdv_case", None):
        record.llmdv_case = nodes[case.nodeid]
        record.msg = f"[{record.llmdv_case}] {record.getMessage()}"
        record.args = ()
    return True


class LostConnections:
    """The connection gate's small shared map, including across stage boundaries."""

    def __init__(self, db):
        self.db = db

    def __bool__(self):
        return bool(self.db.execute("SELECT 1 FROM lost LIMIT 1").fetchone())

    def get(self, ingress):
        row = self.db.execute("SELECT detail FROM lost WHERE ingress = ?", (ingress,)).fetchone()
        return json.loads(row[0]) if row else None

    def __setitem__(self, ingress, detail):
        self.db.execute("INSERT OR IGNORE INTO lost VALUES (?, ?)", (ingress, json.dumps(detail)))

    def pop(self, ingress, default):
        self.db.execute("DELETE FROM lost WHERE ingress = ?", (ingress,))


class Assignment:
    def __init__(self, job, db, lock):
        self.job = job
        self.db = db
        self.lock = lock
        self.maxfail = 0
        self.session_finished = False

    @pytest.hookimpl(trylast=True)
    def pytest_configure(self, config):
        config._llmdv_connection_lost = LostConnections(self.db)
        config._llmdv_concurrency = self.job["concurrency"]
        config._llmdv_stage = self.job["stage"]
        config._llmdv_profile_digest = self.job["profile_digest"]
        config._llmdv_slugs = self.job["slugs"]
        config._llmdv_public_nodes = self.job["nodes"]
        self.maxfail = config.option.maxfail

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, config, items):
        yield
        from ..plugin import _execution_policy

        wanted = self.job["slugs"]
        assigned = [item for item in items if item.nodeid in wanted]
        if [item.nodeid for item in assigned] != list(wanted) or any(
            _execution_policy(item)[0] != self.job["stage"] for item in assigned
        ):
            raise RuntimeError("worker collection differs from the planned assignment")
        config.hook.pytest_deselected(items=[item for item in items if item.nodeid not in wanted])
        items[:] = assigned

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item):
        if self.maxfail:
            failures = self.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
            if failures >= self.maxfail:
                item.session.shouldfail = f"stopping after {failures} failures"
                self.db.execute("INSERT OR IGNORE INTO stopped VALUES (1)")
                return True
        return None

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.db.execute(
                "INSERT INTO failures VALUES (?, ?, ?)",
                (
                    self.job["nodes"][report.nodeid],
                    report.when,
                    redact.scrub(str(report.longrepr))[-2000:],
                ),
            )
            if (
                self.maxfail
                and self.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0] >= self.maxfail
            ):
                self.db.execute("INSERT OR IGNORE INTO stopped VALUES (1)")

    def pytest_runtest_logfinish(self, nodeid):
        self.db.execute("INSERT INTO finished VALUES (?)", (self.job["nodes"][nodeid],))

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self):
        # Each report flushes the existing redacted writers to the public run directory.
        # Only evidence emission takes this lock. Test setup, requests and teardown overlap.
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            # If a writer died mid-report, leave its tail alone for recovery. No
            # surviving writer may append a good record to a possibly torn line.
            if os.fstat(self.lock.fileno()).st_size:
                raise RuntimeError("another worker died while recording evidence")
            self.lock.write(b"1")
            self.lock.flush()
            outcome = yield
            if outcome.excinfo is None:
                self.lock.truncate(0)
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)

    def pytest_sessionfinish(self):
        self.session_finished = True


def _watch_parent(parent, scratch):
    # EOF or a broken socket means the coordinator can no longer stop this worker.
    try:
        with contextlib.suppress(OSError):
            parent.recv(1)
        process.stop_worker_clients(scratch, os.getpid())
    finally:
        os.killpg(os.getpgrp(), signal.SIGKILL)


def main(path: Path, parent_fd: int, secrets_fd: int) -> int:
    parent = socket.socket(fileno=parent_fd)
    parent.set_inheritable(False)
    os.register_at_fork(after_in_child=parent.close)
    # Workers share an open file description. pread leaves the offset untouched for peers.
    with os.fdopen(secrets_fd, "rb") as secrets:
        redact.install_secrets(
            json.loads(os.pread(secrets.fileno(), os.fstat(secrets_fd).st_size, 0))
        )
    job = json.loads(path.read_text())
    scratch = Path(job["run_dir"]) / RUN_STATE
    process.worker_directory = scratch

    threading.Thread(target=_watch_parent, args=(parent, scratch), daemon=True).start()
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})
    style.force_plain(job["no_color"])
    setup.configure(
        level=job["level"],
        fmt=job["format"],
        run_log=Path(job["run_dir"]) / "llmdv.log",
        log_file=Path(job["log_file"]) if job["log_file"] else None,
    )
    label_case = partial(_label_case, nodes=job["nodes"])
    for name in (setup.ROOT, setup.OUTPUT):
        for handler in logging.getLogger(name).handlers:
            handler.addFilter(label_case)
    with (
        contextlib.closing(sqlite3.connect(scratch / "state", isolation_level=None)) as db,
        (scratch / "report.lock").open("ab") as lock,
    ):
        assignment = Assignment(job, db, lock)
        code = _run_pytest(job["argv"], job["raw"], job["setup"], plugins=(assignment,))
        # An os._exit(0), including one after the last result row, cannot forge this receipt.
        if not assignment.session_finished:
            return 3
        path.with_suffix(".done").write_text(str(code))
        return code


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])))
