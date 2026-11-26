"""Bounded live runs. Each stage drains whole pytest sessions before the next begins."""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import yaml

from ..clients.process import stop_worker_clients
from ..evidence.bundle import case_slug
from ..http import redact
from ..log.setup import get_logger
from ..output.local import RUN_STATE

log = get_logger("cli.verify")
STAGES = ("initial", "eligible", "exclusive", "logs")


def assignments(cases, concurrency):
    """Preserve plan order within each fixed shard, and across the serial stages."""
    for stage in STAGES:
        rows = [r for r in cases if r["stage"] == stage]
        width = min(concurrency if stage == "eligible" else 1, len(rows))
        if width:
            yield stage, [rows[i::width] for i in range(width)]


def public_nodes(cases):
    """Keep redacted evidence identities unique even when two names scrub to the same text."""
    nodes = {}
    used = set()
    for index, row in enumerate(cases):
        node = row["nodeid"]
        public = redact.scrub(node)
        while public in used:
            public += f"#{index}"
        used.add(public)
        nodes[node] = public
    return nodes


def _slugs(cases):
    # Preserve established paths wherever possible. Sanitizing and truncating can make
    # different parametrizations identical, even on a case-insensitive filesystem.
    slugs = {}
    used = set()
    for index, row in enumerate(cases):
        base = case_slug(
            row["ingress"] or "noingress", row["case_id"] or row["nodeid"], row["variant"]
        )
        slug = redact.scrub(base)
        while slug.casefold() in used:
            slug += f"--{index}"
        used.add(slug.casefold())
        slugs[row["nodeid"]] = slug
    return slugs


def _signal_group(proc, signum):
    if proc.returncode is None:
        # macOS returns EPERM for an exiting group with only zombies. The bounded
        # wait in _reap still detects a live worker that could not be stopped.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signum)


def _reap(proc, directory):
    # The leader still reserves its PID. Stop any descendants before releasing it.
    _signal_group(proc, signal.SIGKILL)
    try:
        code = proc.wait(timeout=5)
    finally:
        stop_worker_clients(directory, proc.pid)
    (directory / f"stopping-{proc.pid}").unlink(missing_ok=True)
    return code


def _stop(children, directory):
    for proc in children.values():
        _signal_group(proc, signal.SIGTERM)
    deadline = time.monotonic() + 3
    first_timeout = None
    for channel, proc in children.items():
        with contextlib.suppress(OSError):
            channel.settimeout(max(0, deadline - time.monotonic()))
            channel.recv(1)
        try:
            _reap(proc, directory)
        except subprocess.TimeoutExpired as exc:
            first_timeout = first_timeout or exc
    if first_timeout is not None:
        raise first_timeout


def _stage(directory, stage, shards, common, slugs, nodes, secrets_fd):
    children = {}
    failed = False
    with selectors.DefaultSelector() as ready, contextlib.ExitStack() as channels:
        try:
            for index, rows in enumerate(shards):
                path = directory / f"{stage}-{index}.json"
                job = dict(
                    common, stage=stage, slugs={r["nodeid"]: slugs[r["nodeid"]] for r in rows}
                )
                job["nodes"] = {node: nodes[node] for node in job["slugs"]}
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
                channel, peer = socket.socketpair()
                channels.enter_context(channel)
                with peer:
                    ready.register(channel, selectors.EVENT_READ, path)
                    # Record ownership before handling a signal delivered during spawn.
                    mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
                    try:
                        children[channel] = subprocess.Popen(
                            [
                                sys.executable,
                                "-m",
                                "llmdv.cli.live_worker",
                                str(path),
                                str(peer.fileno()),
                                str(secrets_fd),
                            ],
                            pass_fds=(peer.fileno(), secrets_fd),
                            start_new_session=True,
                        )  # noqa: S603 -- our interpreter and private assignment
                    finally:
                        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            while children:
                # Neither side sends data: readability means the worker exited. Unlike
                # Popen.poll(), observing EOF does not reap the process-group leader.
                for key, _ in ready.select():
                    code = _reap(children[key.fileobj], directory.parent)
                    del children[key.fileobj]
                    ready.unregister(key.fileobj)
                    receipt = key.data.with_suffix(".done")
                    if (
                        code not in (0, 1)
                        or not receipt.is_file()
                        or receipt.read_text() != str(code)
                    ):
                        log.error(
                            "verification worker stopped without a successful session (status %s)",
                            code,
                        )
                        return 3  # pytest internal error
                    failed |= code == 1
            return int(failed)
        finally:
            _stop(children, directory.parent)


def _order(path, order, *, results, scratch=None):
    """Atomically restore plan order with one line in memory, keeping original timestamps."""
    if not path.is_file():
        return
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
            offsets[node].append(offset)
        if offsets.keys() - order.keys() or (
            results and any(len(rows) > 1 for rows in offsets.values())
        ):
            raise RuntimeError(f"unexpected or duplicate case in {path.name}")
        with tempfile.NamedTemporaryFile(dir=scratch or path.parent, delete=False) as dest:
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


def _result_ids(path):
    """Only newline-terminated verdicts count when a worker dies during a write."""
    if not path.is_file():
        return set()
    with path.open("rb") as source:
        return {json.loads(line)["nodeid"] for line in source if line.endswith(b"\n")}


def _reconcile(run_dir, execution, db):
    """Put complete evidence rows in plan order and account for the whole pytest protocol."""
    order = {node: i for i, node in enumerate(execution["pending_plan"])}
    scratch = run_dir / RUN_STATE
    # Both a drained stage and recovery after coordinator death have finished using these inputs.
    workers = scratch / "workers"
    if workers.exists():
        shutil.rmtree(workers)
    results = run_dir / "results.jsonl"
    _order(results, order, results=True, scratch=scratch)
    _order(run_dir / "exchanges.jsonl", order, results=False, scratch=scratch)
    seen = _result_ids(results)
    finished = {row[0] for row in db.execute("SELECT node FROM finished")} if db else set()
    unfinished = [node for node in order if node not in finished or node not in seen]
    execution["unfinished"] = unfinished
    if unfinished:
        log.warning("%d planned cases did not finish; see outcome.json", len(unfinished))
    if db:
        teardown = dict(db.execute("SELECT node, reason FROM failures WHERE phase='teardown'"))
        if teardown:
            execution["teardown_errors"] = teardown
    del execution["pending_plan"]
    return bool(finished - seen)


def recover(run_dir, execution):
    """Freeze any report in flight before the job or a later publish reads a killed run."""
    import fcntl

    scratch = run_dir / RUN_STATE
    scratch.mkdir(mode=0o700, exist_ok=True)
    with (scratch / "report.lock").open("ab") as lock, contextlib.ExitStack() as stack:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lock.write(b"1")  # Prevent another writer from starting after recovery releases the lock.
        lock.flush()
        db = None
        state = scratch / "state"
        if state.is_file():
            db = stack.enter_context(contextlib.closing(sqlite3.connect(state)))
        _reconcile(run_dir, execution, db)


def clear_receipts(run_dir):
    scratch = run_dir / RUN_STATE
    (scratch / "state").unlink(missing_ok=True)
    (scratch / "report.lock").unlink(missing_ok=True)


def run(args, run, argv, cases, profile):
    nodes = public_nodes(cases)
    if len(nodes) != len(cases) or any(row["stage"] not in STAGES for row in cases):
        raise ValueError("invalid or duplicate case in the verification plan")
    # Preflight can register another secret that occurs in a planned node ID.
    plan = list(nodes.values())
    if plan != run.outcome.execution["pending_plan"]:
        run.outcome.execution["pending_plan"] = plan
        run.outcome.write(run.run_dir)
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
    scratch = run.run_dir / RUN_STATE
    scratch.mkdir(mode=0o700, exist_ok=True)
    directory = scratch / "workers"
    directory.mkdir(mode=0o700)
    with contextlib.ExitStack() as stack:
        # Freeze the same declarations preflight used, including names and relative client
        # paths. Credentials are still resolved by the normal profile loader from the environment.
        frozen = directory / "profile.yaml"
        with os.fdopen(os.open(frozen, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as file:
            yaml.safe_dump(profile, file, sort_keys=False)
        argv = list(argv)
        argv[argv.index("--llmdv-profile") + 1] = str(frozen)
        common["argv"] = argv
        state = scratch / "state"
        db = stack.enter_context(contextlib.closing(sqlite3.connect(state, isolation_level=None)))
        db.executescript("""
            CREATE TABLE lost (ingress TEXT PRIMARY KEY, detail TEXT);
            CREATE TABLE finished (node TEXT PRIMARY KEY);
            CREATE TABLE failures (node TEXT, phase TEXT, reason TEXT);
            CREATE TABLE stopped (stop INTEGER PRIMARY KEY);
        """)
        # Preflight may register credentials obtained from a service, absent from the profile.
        secrets = stack.enter_context(tempfile.TemporaryFile())
        secrets.write(json.dumps(sorted(redact.known_secrets())).encode())
        secrets.flush()
        code = 0
        stopped = False
        try:
            for stage, shards in assignments(cases, args.concurrency):
                code = max(
                    code,
                    _stage(directory, stage, shards, common, slugs, nodes, secrets.fileno()),
                )
                stopped = bool(db.execute("SELECT 1 FROM stopped").fetchone())
                if code > 1 or stopped:
                    break
        except KeyboardInterrupt:
            code = 2  # pytest's interrupted exit; verify collects logs and publishes
        finally:
            missing = _reconcile(run.run_dir, run.outcome.execution, db)
            if code != 2 and (missing or (run.outcome.execution["unfinished"] and not stopped)):
                code = 3
            # Once this snapshot is durable, recovery no longer needs the local receipts.
            run.outcome.write(run.run_dir)
        if db.execute("SELECT 1 FROM failures LIMIT 1").fetchone():
            code = max(code, 1)
    clear_receipts(run.run_dir)
    return code
