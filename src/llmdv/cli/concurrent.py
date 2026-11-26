"""Bounded live runs. Each stage drains whole pytest sessions before the next begins."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from ..clients.process import stop_worker_clients
from ..evidence.bundle import case_slug
from ..log.setup import get_logger

log = get_logger("cli.verify")
STAGES = ("initial", "eligible", "exclusive", "logs")


def assignments(cases, concurrency):
    """Preserve plan order within each fixed shard, and across the serial stages."""
    for stage in STAGES:
        rows = [r for r in cases if r["stage"] == stage]
        width = min(concurrency if stage == "eligible" else 1, len(rows))
        if width:
            yield stage, [rows[i::width] for i in range(width)]


def _slugs(cases):
    # Preserve established paths wherever possible. Sanitizing and truncating can make
    # different parametrizations identical, even on a case-insensitive filesystem.
    slugs = {}
    used = set()
    for index, row in enumerate(cases):
        base = case_slug(
            row["ingress"] or "noingress", row["case_id"] or row["nodeid"], row["variant"]
        )
        slug = base
        while slug.casefold() in used:
            slug += f"--{index}"
        used.add(slug.casefold())
        slugs[row["nodeid"]] = slug
    return slugs


def _stop(children, directory):
    # A leader can die leaving a client in its process group. Signal the groups even
    # when poll() already observed the leader's exit.
    for proc in children:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    for proc in children:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=max(0, deadline - time.monotonic()))
    for proc in children:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        stop_worker_clients(directory, proc.pid)


def _stage(directory, stage, shards, common, slugs, parent_fd):
    children = {}
    failed = False
    try:
        for index, rows in enumerate(shards):
            path = directory / f"{stage}-{index}.json"
            job = dict(common, stage=stage, slugs={r["nodeid"]: slugs[r["nodeid"]] for r in rows})
            job["argv"] = [
                *common["argv"],
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(directory / f"tmp-{stage}-{index}"),
            ]
            with os.fdopen(
                os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as stream:
                json.dump(job, stream)
            # Record ownership before handling a signal delivered during spawn.
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "llmdv.cli.live_worker", str(path), str(parent_fd)],
                    pass_fds=(parent_fd,),
                    start_new_session=True,
                )  # noqa: S603 -- our interpreter and private assignment
                children[proc] = path
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        pending = dict(children)
        while pending:
            for proc, path in list(pending.items()):
                code = proc.poll()
                if code is None:
                    continue
                receipt = path.with_suffix(".done")
                if code not in (0, 1) or not receipt.is_file() or receipt.read_text() != str(code):
                    log.error(
                        "verification worker stopped without a successful session (status %s)", code
                    )
                    return 3  # pytest internal error
                failed |= code == 1
                del pending[proc]
            if pending:
                time.sleep(0.05)
        return int(failed)
    finally:
        _stop(children, directory)


def _order(path, order, *, results):
    """Atomically restore plan order with one line in memory, keeping original timestamps."""
    seen = Counter()
    if not path.is_file():
        return seen
    offsets = defaultdict(list)
    with path.open("rb") as source:
        while True:
            offset = source.tell()
            line = source.readline()
            if not line:
                break
            # An interrupted write can leave an incomplete last row.
            if not line.endswith(b"\n"):
                log.warning("incomplete final row in %s", path.name)
                break
            node = json.loads(line)["nodeid"]
            seen[node] += 1
            offsets[node].append(offset)
        if set(seen) - order.keys() or (results and any(count != 1 for count in seen.values())):
            raise RuntimeError(f"unexpected or duplicate case in {path.name}")
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as dest:
            try:
                for node in sorted(offsets, key=lambda n: order.get(n, len(order))):
                    for offset in offsets[node]:
                        source.seek(offset)
                        dest.write(source.readline())
                os.fchmod(dest.fileno(), stat.S_IMODE(os.fstat(source.fileno()).st_mode))
                dest.close()
                os.replace(dest.name, path)
            finally:
                Path(dest.name).unlink(missing_ok=True)
    return seen


def run(args, run, argv, cases, profile):
    order = {row["nodeid"]: i for i, row in enumerate(cases)}
    if len(order) != len(cases) or any(row["stage"] not in STAGES for row in cases):
        raise ValueError("invalid or duplicate case in the verification plan")
    slugs = _slugs(cases)
    common = dict(
        setup=run.setup_commands,
        raw=args.raw_pytest,
        concurrency=args.concurrency,
        run_dir=str(run.run_dir),
        level=args.log_level,
        format=args.log_format,
        log_file=args.log_file,
        no_color=args.no_color,
        profile_digest=run.outcome.identity.get("profile_digest"),
    )
    with tempfile.TemporaryDirectory(prefix="llmdv-") as temporary, contextlib.ExitStack() as stack:
        directory = Path(temporary)
        # Freeze the same declarations preflight used, including names and relative client
        # paths. Credentials are still resolved by the normal profile loader from the environment.
        frozen = directory / "profile.yaml"
        with os.fdopen(os.open(frozen, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as file:
            yaml.safe_dump(profile, file, sort_keys=False)
        argv = list(argv)
        argv[argv.index("--llmdv-profile") + 1] = str(frozen)
        common["argv"] = argv
        db = stack.enter_context(
            contextlib.closing(sqlite3.connect(directory / "state", isolation_level=None))
        )
        db.executescript("""
            CREATE TABLE lost (ingress TEXT PRIMARY KEY, detail TEXT);
            CREATE TABLE finished (node TEXT PRIMARY KEY);
            CREATE TABLE failures (node TEXT, phase TEXT, reason TEXT);
            CREATE TABLE stopped (stop INTEGER PRIMARY KEY);
        """)
        read_fd, write_fd = os.pipe()
        stack.callback(os.close, write_fd)
        stack.callback(os.close, read_fd)
        code = 0
        stopped = False
        try:
            for stage, shards in assignments(cases, args.concurrency):
                code = max(code, _stage(directory, stage, shards, common, slugs, read_fd))
                stopped = bool(db.execute("SELECT 1 FROM stopped").fetchone())
                if code > 1 or stopped:
                    break
        except KeyboardInterrupt:
            code = 2  # pytest's interrupted exit; verify collects logs and publishes
        finally:
            seen = _order(run.run_dir / "results.jsonl", order, results=True)
            _order(run.run_dir / "exchanges.jsonl", order, results=False)
            finished = {row[0] for row in db.execute("SELECT node FROM finished")}
            unfinished = [node for node in order if node not in finished or node not in seen]
            run.outcome.execution["unfinished"] = unfinished
            if finished - seen.keys():
                # Fail-fast only excuses cases pytest never finished. A finished case
                # without a result is always lost evidence.
                if code != 2:
                    code = 3
            if unfinished:
                log.warning("%d planned cases did not finish; see outcome.json", len(unfinished))
                if not stopped and code != 2:
                    code = 3
            teardown = dict(db.execute("SELECT node, reason FROM failures WHERE phase='teardown'"))
            if teardown:
                run.outcome.execution["teardown_errors"] = teardown
        if db.execute("SELECT 1 FROM failures LIMIT 1").fetchone():
            code = max(code, 1)
        return code
