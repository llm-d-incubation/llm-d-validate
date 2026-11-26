"""Exercise the public commands across a shared fake backend and real worker processes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from llmdv import execution
from llmdv.cli import concurrent, verify_cmd
from llmdv.cli.main import build_parser
from llmdv.compare.diff import compare_window
from llmdv.http import redact
from tests.fakes import SRC, FakeFleet, FakeS3, FakeSlack, only_run, profile_yaml, run_llmdv, s3_env
from tests.fleet_fakes import (
    LOGS_INGRESS_YAML,
    FakePrometheus,
    fake_kubectl,
    fleet_state,
    kubectl_calls,
    metrics_environment,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="concurrent live workers require POSIX")

ELIGIBLE = (
    "openai_chat.basic_non_streaming",
    "openai_completions.basic_non_streaming",
    "anthropic_messages.basic_non_streaming",
    "openai_responses.basic_non_streaming",
)
SELECTION = (
    "contract.declared_surfaces_exist",
    "discovery.models_lists_declared_model",
    *ELIGIBLE,
    "transport.sdk_retry_signal",
    "unhappy.invalid_token_limit",
    "cache.native_reuse",
    "logs.signatures",
)


def profile(tmp_path, fleet, cases, *, shared_backend=False):
    path = tmp_path / "profile.yaml"
    raw = yaml.safe_load(profile_yaml(fleet.base_url, fleet.model))
    raw["test_sets"] = {"default": list(cases)}
    if shared_backend:
        raw["ingresses"].append(dict(raw["ingresses"][0], id="router", kind="router"))
    path.write_text(yaml.safe_dump(raw))
    return path


def rows(run, file="results.jsonl"):
    return [json.loads(line) for line in (run / file).read_text().splitlines()]


def verify(tmp_path, path, width, *extra, env=None):
    root = tmp_path / f"runs-{width}"
    result = run_llmdv(
        "verify",
        "--profile",
        str(path),
        "--local-output",
        str(root),
        "--concurrency",
        str(width),
        "--pod-logs",
        "off",
        *extra,
        env={"TMPDIR": str(tmp_path), **(env or {})},
        timeout=90,
    )
    return result, only_run(root)


@pytest.mark.parametrize("command", ["verify", "job"])
@pytest.mark.parametrize("value", ["0", "-2", "auto", "1.5"])
def test_positive_integer_required(command, value):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([command, "--profile", "unused", "--concurrency", value])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("option", "via_env"),
    [
        ("--junitxml", False),
        ("--junit-xml", True),
        ("log_file", False),
        ("--debug", False),
        ("--debug", True),
    ],
)
def test_shared_pytest_output_is_refused_before_preflight(tmp_path, option, via_env):
    output = tmp_path / "ci-output"
    output.write_text("previous report")
    # Exercise the parsed pytest configuration, including aliases and environment options.
    env = {}
    extra = []
    if via_env:
        env["PYTEST_ADDOPTS"] = f'{option} "{output}"'
    elif option == "log_file":
        extra = ["-o", f"log_file={output}"]
    else:
        extra = [f"{option}={output}"]
    with FakeFleet() as fleet:
        path = profile(tmp_path, fleet, ELIGIBLE[:2])
        result, _ = verify(tmp_path, path, 2, *extra, env=env)
        assert result.returncode == 2, result.stderr
        assert "--concurrency 1" in result.stderr
        assert fleet.seen == []
    assert output.read_text() == "previous report"


def test_serial_junit_keeps_every_case_and_failure(tmp_path):
    output = tmp_path / "ci.xml"
    with FakeFleet(abort_finish_reason=True) as fleet:
        path = profile(tmp_path, fleet, ELIGIBLE[:2])
        result, run = verify(tmp_path, path, 1, f"--junitxml={output}")
    assert result.returncode == 1, result.stderr
    report = ET.parse(output)
    records = rows(run)
    assert len(report.findall(".//testcase")) == len(records) == 2
    assert len(report.findall(".//failure")) == sum(r["outcome"] == "fail" for r in records)


@pytest.mark.parametrize(
    "option", ["--setup-only", "--setupplan", "--co", "--fixtures-per-test", "--fixtures"]
)
def test_diagnostic_modes_match_the_serial_session(tmp_path, option):
    with FakeFleet() as fleet:
        path = profile(tmp_path, fleet, [*ELIGIBLE[:2], "logs.signatures"])
        skips = []
        for width in (1, 2):
            extra = ["--", option] if option in ("--setup-only", "--fixtures-per-test") else []
            env = {} if extra else {"PYTEST_ADDOPTS": option}
            result, run = verify(tmp_path, path, width, "--skip-preflight", *extra, env=env)
            assert result.returncode == 0, result.stderr
            if option.startswith("--fixtures"):
                assert "fleet_kube" in (run / "llmdv.log").read_text()
            assert "unfinished" not in json.loads((run / "outcome.json").read_text())["execution"]
            skips.append([(r["nodeid"], r["outcome"], r["skip_class"]) for r in rows(run)])
        assert not fleet.seen
    assert skips[0] == skips[1]
    if option == "--setup-only":
        assert skips[0] and all(outcome == "skip" for _, outcome, _ in skips[0])


def test_stage_never_signals_a_reaped_worker(tmp_path, monkeypatch):
    workers = {}

    def spawn(argv, **kwargs):
        proc = Mock(pid=len(workers) + 1, returncode=None)
        workers[proc.pid] = proc

        def reap(*args, **kwargs):
            proc.returncode = 0
            return 0

        proc.poll.side_effect = proc.wait.side_effect = reap
        Path(argv[3]).with_suffix(".done").write_text("0")
        return proc

    def signal_group(pid, signum):
        assert workers[pid].returncode is None, "the OS can already reuse this PID"

    monkeypatch.setattr(concurrent.subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "killpg", signal_group)
    directory = tmp_path / "workers"
    directory.mkdir()
    nodes = {"a": "a", "b": "b"}
    shards = [[{"nodeid": node}] for node in nodes]
    assert concurrent._stage(directory, "eligible", shards, {"argv": []}, nodes, nodes, -1) == 0
    assert all(proc.returncode == 0 for proc in workers.values())


def test_stop_reaps_peers_after_a_worker_timeout(tmp_path, monkeypatch):
    stuck = Mock(pid=1, returncode=None)
    stuck.wait.side_effect = subprocess.TimeoutExpired("worker", 5)
    peer = Mock(pid=2, returncode=None)
    monkeypatch.setattr(os, "killpg", Mock())
    with pytest.raises(subprocess.TimeoutExpired) as error:
        concurrent._stop({Mock(): stuck, Mock(): peer}, tmp_path)
    assert error.value is stuck.wait.side_effect
    assert (tmp_path / "stopping-1").exists()
    peer.wait.assert_called_once_with(timeout=5)


def test_broken_parent_socket_stops_worker_and_clients(tmp_path, monkeypatch):
    from llmdv.cli import live_worker

    parent = Mock()
    parent.recv.side_effect = ConnectionResetError()
    kill = Mock()
    monkeypatch.setattr(os, "killpg", kill)
    live_worker._watch_parent(parent, tmp_path)
    assert (tmp_path / f"stopping-{os.getpid()}").is_file()
    kill.assert_called_once_with(os.getpgrp(), signal.SIGKILL)


def test_policy_covers_catalog_and_fails_closed():
    result = run_llmdv(
        "plan",
        "--profile",
        "profiles/examples/example-two-ingress.yaml",
        "--ingress",
        "vllm,router",
        "--full-suite",
        "--json",
        env={"TMPDIR": os.environ.get("TMPDIR", "/tmp")},
    )
    assert result.returncode == 0, result.stderr
    catalog = json.loads(result.stdout)["cases"]
    assert {r["case_id"] for r in catalog} == execution.CATALOG.keys()
    for r in catalog:
        assert r["stage_reason"]
        if (
            r["suite"] in {"cache", "fleet", "scenarios", "long_context", "real_clients"}
            or "disruptive" in r["markers"]
        ):
            assert r["stage"] in ("exclusive", "logs")
        if r["case_id"] in (
            "streaming.steady_flow",
            "openai_chat.greedy_repeat",
            "openai_chat.seed_repeat",
            "openai_chat.streaming_matches_json",
            "openai_completions.streaming_matches_json",
            "anthropic_messages.stream_matches_message",
            "tools.stream_matches_json",
            "anthropic_messages.incremental_stream",
        ):
            assert r["stage"] == "exclusive"
    assert execution.policy("extension.new", "extension", None)[0] == "exclusive"
    with pytest.raises(ValueError, match="conflicts"):
        execution.policy(ELIGIBLE[0], "openai_chat", "needs silence")


def test_shard_boundaries_and_unique_evidence_paths():
    cases = [
        dict(nodeid=str(i), stage=stage, case_id="x.y", ingress="model", variant=v)
        for i, (stage, v) in enumerate(
            [
                ("exclusive", "a/b"),
                ("eligible", "a_b"),
                ("logs", "A_B"),
                ("initial", "x" * 150),
                ("exclusive", "x" * 151),
                ("eligible", "a_b--1"),
            ]
        )
    ]
    stages = list(concurrent.assignments(cases, 50))
    assert [(s, len(shards)) for s, shards in stages] == [
        ("initial", 1),
        ("eligible", 2),
        ("exclusive", 1),
        ("logs", 1),
    ]
    assert stages[2][1][0] == [cases[0], cases[4]]
    assert len(set(v.casefold() for v in concurrent._slugs(cases).values())) == len(cases)


def test_public_ids_remain_distinct_after_redaction(monkeypatch):
    monkeypatch.setattr(redact, "_SECRETS", {"first-key", "second-key"})
    nodes = ["case[first-key]", "case[second-key]", "case[second-key]#1"]
    public = concurrent.public_nodes([{"nodeid": node} for node in nodes])
    assert list(public) == nodes
    assert len(set(public.values())) == len(nodes)
    assert all(redact.SENTINEL in value and "-key" not in value for value in public.values())


def test_short_bearer_key_keeps_assignments_evidence_and_verbose_output(tmp_path):
    with FakeFleet(auth_header=("authorization", "Bearer test")) as fleet:
        path = profile(tmp_path, fleet, ELIGIBLE[:2])
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["auth"] = {"type": "bearer", "key_env": "FAKE_KEY"}
        path.write_text(yaml.safe_dump(raw))
        result, run = verify(
            tmp_path,
            path,
            2,
            "--skip-preflight",
            "-v",
            "--evidence",
            "all",
            env={"FAKE_KEY": "test"},
        )
    assert result.returncode == 0, result.stderr
    records = rows(run)
    assert [r["case_id"] for r in records] == list(ELIGIBLE[:2])
    assert json.loads((run / "outcome.json").read_text())["execution"]["unfinished"] == []
    exchanges = rows(run, "exchanges.jsonl")
    assert {e["nodeid"] for e in exchanges} == {r["nodeid"] for r in records}
    for record in records:
        node = record["nodeid"]
        assert record["outcome"] == "pass" and "test_" not in node
        assert json.loads((run / record["evidence"] / "meta.json").read_text())["nodeid"] == node
        lines = [line for line in result.stderr.splitlines() if f"[{node}]" in line]
        assert any("basic_non_streaming" in line.split(f"[{node}]")[1] for line in lines)
        assert any(f"ok: {record['checks'][0]['name']}=" in line for line in lines)


@pytest.mark.parametrize(("exit_code", "verdict"), [(6, "interrupted"), (3, "environment")])
def test_preflight_stop_keeps_every_planned_case(tmp_path, monkeypatch, exit_code, verdict):
    root = tmp_path / "runs"
    with FakeFleet(serve_chat=False) as fleet:
        path = profile(tmp_path, fleet, ELIGIBLE[:2])
        plan = run_llmdv("plan", "--profile", str(path), "--json", env={"TMPDIR": str(tmp_path)})
        assert plan.returncode == 0, plan.stderr
        expected = [case["nodeid"] for case in json.loads(plan.stdout)["cases"]]
        assert len(expected) == 2

        def interrupt(*args, **kwargs):
            snapshot = json.loads((only_run(root) / "outcome.json").read_text())
            assert snapshot["execution"]["pending_plan"] == expected
            raise KeyboardInterrupt

        args = build_parser().parse_args(
            ["verify", "--profile", str(path), "--local-output", str(root), "--concurrency", "2"]
        )
        if exit_code == 6:
            monkeypatch.setattr(verify_cmd.preflight, "run", interrupt)
        monkeypatch.setattr(verify_cmd.signal, "signal", lambda *args: None)
        monkeypatch.setattr(verify_cmd.log_setup, "configure", lambda **kwargs: None)
        monkeypatch.setattr(verify_cmd.log_setup, "close_run_log", lambda: None)
        assert verify_cmd.cmd_verify(args) == exit_code
    outcome = json.loads((only_run(root) / "outcome.json").read_text())
    assert outcome["verification"]["class"] == verdict
    assert outcome["execution"] == {"concurrency": 2, "planned": 2, "unfinished": expected}


def test_real_overlap_preflight_barriers_and_evidence(tmp_path):
    events = []
    rendezvous = threading.Barrier(2)
    waiting = []
    lock = threading.Lock()

    @contextmanager
    def watch(case, path):
        start = time.monotonic()
        with lock:
            meet = width == 2 and case in ELIGIBLE and len(waiting) < 2
            if meet:
                waiting.append(case)
        if meet:
            rendezvous.wait(timeout=15)
        try:
            yield
        finally:
            events.append((start, time.monotonic(), case, path))

    with FakeFleet(request_window=watch) as fleet:
        path = profile(tmp_path, fleet, SELECTION, shared_backend=True)
        destination = (tmp_path / "archive").as_uri()
        measurements = {}
        runs = {}
        for width in (1, 2):
            events.clear()
            result, run = verify(
                tmp_path,
                path,
                width,
                "--evidence",
                "all",
                "--analyze",
                "--remote-output",
                destination,
            )
            assert result.returncode == 0, result.stderr
            measurements[width] = list(events)
            runs[width] = rows(run)
            assert (run / "dashboard.html").is_file()

    # The same complete preflight through both ingresses, before any selected case.
    def preflight(events):
        return Counter(path for _, _, case, path in events if case is None)

    assert preflight(measurements[1]) == preflight(measurements[2])
    assert preflight(measurements[2])["/v1/models"] == 2
    stages = {r["case_id"]: r["stage"] for r in runs[2]}
    for width, requests in measurements.items():
        sweep = sorted(
            (at, delta, case)
            for start, end, case, _ in requests
            for at, delta in [(start, 1), (end, -1)]
        )
        active = 0
        peak = 0
        for _, change, _ in sweep:
            active += change
            peak = max(peak, active)
        # This selection has no cases with internal request fan-out.
        assert peak == width
    requests = sorted(measurements[2])
    for i, (_, end, case, _) in enumerate(requests):
        if stages.get(case) == "eligible":
            assert all(
                stages.get(other) == "eligible"
                for start, _, other, _ in requests[i + 1 :]
                if start < end
            )
        elif i + 1 < len(requests):
            assert end <= requests[i + 1][0]
    order = [stages.get(case, "preflight") for _, _, case, _ in requests]
    assert order == sorted(order, key=["preflight", "initial", "eligible", "exclusive"].index)
    assert len(waiting) == 2

    def semantics(records):
        return [
            (r["nodeid"], r["outcome"], r["skip_class"], [c["name"] for c in r["checks"]])
            for r in records
        ]

    assert semantics(runs[1]) == semantics(runs[2])
    assert all(r["stage"] == "serial" for r in runs[1])
    assert all(r["concurrency"] == 2 for r in runs[2])
    assert all(r["stage"] == "exclusive" for r in runs[2] if r["case_id"].startswith("cache."))
    exchanges = defaultdict(list)
    for exchange in rows(run, "exchanges.jsonl"):
        exchanges[exchange["nodeid"]].append(exchange)
    for record in runs[2]:
        bundle = run / record["evidence"]
        assert json.loads((bundle / "meta.json").read_text())["nodeid"] == record["nodeid"]
        case_exchanges = exchanges[record["nodeid"]]
        assert [e["seq"] for e in case_exchanges] == list(range(len(case_exchanges)))
        assert all(
            e["case_id"] == record["case_id"] and e["run_id"] == record["run_id"]
            for e in case_exchanges
        )
        if case_exchanges:
            assert (bundle / "repro.sh").is_file()
    delta = compare_window([runs[1]], runs[2])
    assert delta.execution_changes and not delta.newly_failing
    compared = run_llmdv(
        "compare", "--from", destination, "--profile", "fake", env={"TMPDIR": str(tmp_path)}
    )
    assert compared.returncode == 0, compared.stderr
    assert "Case execution conditions changed" in compared.stdout


def worker_plugin(tmp_path, body):
    (tmp_path / "session_fault.py").write_text(body)
    return {"PYTHONPATH": os.pathsep.join([str(SRC), str(tmp_path)])}


def test_job_recovers_plan_order_and_incomplete_teardown_after_coordinator_kill(tmp_path):
    # The eligible cases run first, but follow Chat in the presented plan. Kill after the
    # exclusive call has a result and before teardown can record successful completion.
    plugin = worker_plugin(
        tmp_path,
        """
import os, signal, time
def pytest_runtest_teardown(item):
    if item.config._llmdv_stage == 'exclusive':
        os.kill(os.getppid(), signal.SIGKILL)
        time.sleep(30)
""",
    )
    exclusive = "openai_chat.greedy_repeat"
    completed = [ELIGIBLE[1], ELIGIBLE[3]]
    dest = tmp_path / "published"
    with FakeFleet() as fleet:
        path = profile(tmp_path, fleet, [exclusive, *completed, "logs.signatures"])
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["chat_repeats"] = ["greedy"]
        path.write_text(yaml.safe_dump(raw))
        result = run_llmdv(
            "job",
            "--profile",
            str(path),
            "--concurrency",
            "2",
            "--work-dir",
            str(tmp_path / "work"),
            "--remote-output",
            dest.as_uri(),
            "--pod-logs",
            "off",
            "-p",
            "session_fault",
            env={**plugin, "TMPDIR": str(tmp_path)},
            timeout=90,
        )
    assert result.returncode == 137, result.stderr
    assert not list(tmp_path.glob("llmdv-*/profile.yaml"))
    assert not (only_run(tmp_path / "work") / ".llmdv-started" / "workers").exists()
    [manifest] = dest.rglob("MANIFEST.json")
    assert not any(".llmdv-started" in p.parts for p in manifest.parent.rglob("*"))
    for run in (only_run(tmp_path / "work"), manifest.parent):
        outcome = json.loads((run / "outcome.json").read_text())
        assert outcome["verification"]["class"] == "crashed"
        records = rows(run)
        assert [r["case_id"] for r in records] == [exclusive, *completed]
        assert [*dict.fromkeys(e["case_id"] for e in rows(run, "exchanges.jsonl"))] == [
            exclusive,
            *completed,
        ]
        unfinished = outcome["execution"]["unfinished"]
        assert unfinished[0] == records[0]["nodeid"]
        assert len(unfinished) > 1 and all("test_signatures" in node for node in unfinished[1:])
        assert outcome["execution"]["planned"] == len(completed) + len(unfinished)
        assert "pending_plan" not in outcome["execution"]


@pytest.mark.parametrize(("fault", "expected"), [("crash", 4), ("setup", 1), ("teardown", 1)])
def test_failed_sessions_keep_evidence_and_publish_once(tmp_path, fault, expected):
    plugin = worker_plugin(
        tmp_path,
        """
import os
def pytest_runtest_setup(item):
    if item.config._llmdv_stage == 'exclusive':
        if os.environ['FAULT'] == 'crash':
            os._exit(0)
        if os.environ['FAULT'] == 'setup':
            raise ValueError('setup failed')
def pytest_runtest_teardown(item):
    if item.config._llmdv_stage == 'exclusive' and os.environ['FAULT'] == 'teardown':
        raise ValueError('teardown failed')
""",
    )
    with FakeFleet() as fleet, FakeSlack() as slack:
        path = profile(tmp_path, fleet, [*ELIGIBLE, "unhappy.invalid_token_limit"])
        dest = tmp_path / "published"
        result = run_llmdv(
            "job",
            "--profile",
            str(path),
            "--concurrency",
            "2",
            "--work-dir",
            str(tmp_path / "work"),
            "--remote-output",
            dest.as_uri(),
            "--pod-logs",
            "off",
            "-p",
            "session_fault",
            env={
                **plugin,
                "FAULT": fault,
                "TMPDIR": str(tmp_path),
                "LLMDV_NOTIFY_WEBHOOK": slack.base_url + "/services/test",
            },
            timeout=180,
        )
        assert result.returncode == expected, result.stderr
        assert len(slack.received) == 1
    run = only_run(tmp_path / "work")
    outcome = json.loads((run / "outcome.json").read_text())
    assert outcome["verification"]["exit_code"] == expected
    assert len(list(dest.rglob("MANIFEST.json"))) == 1
    assert {r["case_id"] for r in rows(run) if r["outcome"] == "pass"} >= set(ELIGIBLE)
    if fault == "crash":
        assert outcome["execution"]["unfinished"]
        assert "INTERNAL ERROR (exit 4," in result.stdout
    if fault == "teardown":
        assert outcome["execution"]["teardown_errors"]
        assert "teardown error" in result.stdout
    if fault != "setup":
        assert "PASSED" not in result.stdout
        assert ("unfinished" if fault == "crash" else "teardown error") in (
            run / "summary.md"
        ).read_text()


@pytest.mark.parametrize(
    ("boundary", "signum", "missing_receipt"),
    [
        ("_order", signal.SIGTERM, False),
        ("_order", signal.SIGTERM, True),
        ("_reconcile", signal.SIGINT, False),
    ],
)
def test_signal_during_finalization_uses_worker_receipts(
    tmp_path, monkeypatch, boundary, signum, missing_receipt
):
    root = tmp_path / "runs"
    if missing_receipt:
        reconcile = concurrent._reconcile

        def lose_receipt(run_dir, execution, db):
            # The first case recorded its verdict, but never finished teardown.
            node = execution["pending_plan"][0]
            db.execute("DELETE FROM finished WHERE node = ?", (node,))
            return reconcile(run_dir, execution, db)

        monkeypatch.setattr(concurrent, "_reconcile", lose_receipt)

    original = getattr(concurrent, boundary)

    def interrupt(*args, **kwargs):
        signal.raise_signal(signum)
        return original(*args, **kwargs)

    monkeypatch.setattr(concurrent, boundary, interrupt)
    monkeypatch.setattr(verify_cmd, "_SIGNALLED", threading.Event())
    monkeypatch.setattr(verify_cmd.log_setup, "configure", lambda **kwargs: None)
    monkeypatch.setattr(verify_cmd.log_setup, "close_run_log", lambda: None)
    with FakeFleet() as fleet, ExitStack() as restore:
        for sig in (signal.SIGTERM, signal.SIGINT):
            restore.callback(signal.signal, sig, signal.getsignal(sig))
        path = profile(tmp_path, fleet, ELIGIBLE[:2])
        args = build_parser().parse_args(
            [
                "verify",
                "--profile",
                str(path),
                "--local-output",
                str(root),
                "--concurrency",
                "2",
                "--skip-preflight",
                "--pod-logs",
                "off",
            ]
        )
        assert verify_cmd.cmd_verify(args) == 6
    run = only_run(root)
    outcome = json.loads((run / "outcome.json").read_text())
    assert outcome["verification"]["class"] == "interrupted"
    records = rows(run)
    assert outcome["execution"]["unfinished"] == ([records[0]["nodeid"]] if missing_receipt else [])
    assert [r["case_id"] for r in records if r["outcome"] == "pass"] == list(ELIGIBLE[:2])


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_interruption_drains_workers_and_publishes_completed_cases(tmp_path, signum):
    plugin = worker_plugin(
        tmp_path,
        """
import os, signal
def pytest_runtest_setup(item):
    if item.config._llmdv_stage == 'exclusive':
        os.kill(os.getppid(), int(os.environ['SIGNUM']))
""",
    )
    with FakeFleet() as fleet:
        path = profile(tmp_path, fleet, [*ELIGIBLE, "unhappy.invalid_token_limit"])
        dest = tmp_path / "published"
        result, run = verify(
            tmp_path,
            path,
            2,
            "-p",
            "session_fault",
            "--remote-output",
            dest.as_uri(),
            env={**plugin, "SIGNUM": str(int(signum))},
        )
    assert result.returncode == 6, result.stderr
    assert "INTERRUPTED (exit 6" in result.stdout and "PASSED" not in result.stdout
    outcome = json.loads((run / "outcome.json").read_text())
    assert outcome["verification"]["class"] == "interrupted"
    assert set(ELIGIBLE) <= {r["case_id"] for r in rows(run)}
    assert len(list(dest.rglob("MANIFEST.json"))) == 1


def test_global_fail_fast_preserves_failure(tmp_path):
    with FakeFleet(abort_finish_reason=True) as fleet:
        path = profile(tmp_path, fleet, [*ELIGIBLE, "unhappy.invalid_token_limit"])
        result, run = verify(tmp_path, path, 2, "-x")
    assert result.returncode == 1, result.stderr
    records = rows(run)
    # Either worker can fail first, and a case already in flight can finish.
    assert 1 <= len(records) <= 2
    assert all(r["case_id"] in ELIGIBLE and r["outcome"] == "fail" for r in records)
    assert all(r["stage"] == "eligible" for r in records)
    assert json.loads((run / "outcome.json").read_text())["execution"]["unfinished"]


def test_connection_loss_reaches_later_stage(tmp_path):
    with FakeFleet(go_dark_after=2) as fleet:
        path = profile(tmp_path, fleet, [*ELIGIBLE, "unhappy.invalid_token_limit"])
        result, run = verify(tmp_path, path, 2, "--skip-preflight")
    assert result.returncode == 3, result.stderr
    assert any(r["connection_lost"] and r["outcome"] == "error" for r in rows(run))
    assert all(
        r["skip_class"] == "requires_instrumentation"
        for r in rows(run)
        if r["stage"] == "exclusive"
    )


def test_log_scan_redacts_preflight_credentials_in_publication(tmp_path):
    pytest.importorskip("boto3")
    token = "test_signatures"  # Also occurs in the log cases' parametrized node IDs.
    with FakeFleet() as fleet, FakePrometheus({}, token=token) as prom, FakeS3() as store:
        path = profile(tmp_path, fleet, ["logs.signatures"])
        raw = yaml.safe_load(path.read_text())
        raw.update(yaml.safe_load(metrics_environment(prom.base_url, "METRICS_KEY")))
        raw["environment"]["namespace"] = "fleet"
        raw["ingresses"][0].update(yaml.safe_load(LOGS_INGRESS_YAML))
        path.write_text(yaml.safe_dump(raw))
        env = s3_env(store)
        storage_key = env["AWS_SECRET_ACCESS_KEY"]
        state = fleet_state()
        for source in state["logs"]:
            state["logs"][source] += f"INFO x-api-key={token} storage-key={storage_key}\n"
        result, run = verify(
            tmp_path,
            path,
            2,
            "--remote-output",
            "s3://results/run",
            env={**env, **fake_kubectl(tmp_path / "bin", state), "METRICS_KEY": token},
        )
    assert result.returncode == 0, result.stderr
    logs = [p.read_bytes() for p in (run / "log-scan").glob("*.log")]
    published = [body for key, body in store.objects.items() if "/log-scan/" in key]
    assert len(logs) == len(published) == 3
    expected = f"x-api-key={redact.SENTINEL} storage-key={redact.SENTINEL}".encode()
    assert all(expected in body for body in logs + published)
    assert all(
        token.encode() not in body and storage_key.encode() not in body
        for body in store.objects.values()
    )


def test_log_scan_redacts_metrics_token_without_preflight(tmp_path):
    token = "test-metrics-secret"
    published = tmp_path / "published"
    with FakeFleet() as fleet, FakePrometheus({"ready": [({}, 1)]}, token=token) as prom:
        path = profile(tmp_path, fleet, ["metrics.bounds", "logs.signatures"])
        raw = yaml.safe_load(path.read_text())
        raw.update(yaml.safe_load(metrics_environment(prom.base_url, "METRICS_KEY")))
        raw["environment"]["namespace"] = "fleet"
        raw["fleet"] = {"metrics": [{"id": "ready", "check": "bounds", "query": "ready", "min": 1}]}
        raw["ingresses"][0].update(yaml.safe_load(LOGS_INGRESS_YAML))
        path.write_text(yaml.safe_dump(raw))
        state = fleet_state()
        for source in state["logs"]:
            state["logs"][source] += f"INFO x-api-key={token}\n"
        result, run = verify(
            tmp_path,
            path,
            2,
            "--skip-preflight",
            "--remote-output",
            published.as_uri(),
            env={**fake_kubectl(tmp_path / "bin", state), "METRICS_KEY": token},
        )
    assert result.returncode == 0, result.stderr
    assert prom.requests == [("GET", "/api/v1/query", "ready")]
    for root in (run, published):
        logs = list(root.rglob("log-scan/*.log"))
        assert len(logs) == 3
        for path in logs:
            body = path.read_text()
            assert token not in body and f"x-api-key={redact.SENTINEL}" in body


def test_cross_ingress_evidence_and_log_window(tmp_path):
    key = "llmdv-test-only-secret-210"
    with FakeFleet(
        cache_probe=True, cache_block=16, auth_header=("authorization", "Bearer " + key)
    ) as fleet:
        path = profile(
            tmp_path,
            fleet,
            [ELIGIBLE[0], "cache.cross_ingress", "logs.signatures"],
            shared_backend=True,
        )
        raw = yaml.safe_load(path.read_text())
        raw["environment"] = {"namespace": "fleet"}
        raw["ingresses"][0].update(yaml.safe_load(LOGS_INGRESS_YAML))
        raw["capabilities"]["cache_reuse"] = {
            "surfaces": ["chat_completions"],
            "prefix_words": 128,
            "min_ratio": 0.8,
            "affinity": "routed",
            "block_size": 16,
            "shared_with": ["fake", "router"],
        }
        for ingress in raw["ingresses"]:
            ingress["auth"] = {"type": "bearer", "key_env": "FAKE_KEY"}
        path.write_text(yaml.safe_dump(raw))
        state = fleet_state()
        state["logs"]["engine-0/vllm"] += f"INFO x-api-key={key}\n"
        env = fake_kubectl(tmp_path / "bin", state)
        dest = tmp_path / "published"
        result, run = verify(
            tmp_path,
            path,
            2,
            "--skip-preflight",
            "--pod-logs",
            "all",
            "--evidence",
            "all",
            "--remote-output",
            dest.as_uri(),
            env={**env, "FAKE_KEY": key},
        )
    assert result.returncode == 0, result.stderr
    assert key not in result.stderr + result.stdout
    assert any(
        f"x-api-key={redact.SENTINEL}" in p.read_text() for p in (run / "logs").glob("*.log")
    )
    assert all(key.encode() not in p.read_bytes() for p in dest.rglob("*") if p.is_file())
    records = rows(run)
    checked = [
        r for r in records if r["case_id"] == "cache.cross_ingress" and r["outcome"] == "pass"
    ]
    assert len(checked) == 2
    # Each case's evidence covers both client instances, including its peer's exchange.
    for r in checked:
        exchanges = [e for e in rows(run, "exchanges.jsonl") if e["nodeid"] == r["nodeid"]]
        assert [e["seq"] for e in exchanges] == list(range(len(exchanges)))
        assert {e["ingress"] for e in exchanges} == {"fake", "router"}
        assert json.loads((run / r["evidence"] / "checks.json").read_text()) == r["checks"]
    logs = [r for r in records if r["stage"] == "logs"]
    assert logs and all(r["outcome"] == "pass" for r in logs)
    assert min(r["ts"] for r in logs) >= max(r["ts"] for r in records if r["stage"] != "logs")
    windows = {
        flag
        for call in kubectl_calls(env)
        if "logs" in call
        for flag in call
        if flag.startswith("--since-time=")
    }
    assert len(windows) == 1
    assert next(iter(windows)).split("=")[1] <= min(r["ts"] for r in records).split(".")[0] + "Z"


def test_delivery_failure_keeps_fleet_exit_and_replay(tmp_path):
    pytest.importorskip("boto3")
    with (
        FakeFleet(abort_finish_reason=True) as fleet,
        FakeS3(deny_after=3) as store,
        FakeSlack() as slack,
    ):
        path = profile(tmp_path, fleet, ELIGIBLE)
        result = run_llmdv(
            "job",
            "--profile",
            str(path),
            "--work-dir",
            str(tmp_path / "work"),
            "--concurrency",
            "2",
            "--pod-logs",
            "off",
            "--remote-output",
            "s3://results/run",
            env={
                **s3_env(store),
                "TMPDIR": str(tmp_path),
                "LLMDV_NOTIFY_WEBHOOK": slack.base_url + "/services/test",
            },
            timeout=180,
        )
        assert len(slack.received) == 1
        assert "results NOT published" in slack.received[0]["text"]
    assert result.returncode == 1, result.stderr
    run = only_run(tmp_path / "work")
    assert "--concurrency 2" in (run / "reproduce.sh").read_text()
    failures = [r for r in rows(run) if r["outcome"] == "fail"]
    assert failures and all(any(not c["ok"] for c in r["checks"]) for r in failures)


@pytest.mark.parametrize("client_group", ["worker", "separate"])
def test_crashed_worker_cleans_its_client_group(tmp_path, client_group):
    import fcntl

    locked = tmp_path / "client.lock"
    plugin = worker_plugin(
        tmp_path,
        """
import os, subprocess, sys, time
from pathlib import Path
from llmdv.clients import process

def pytest_runtest_setup(item):
    if item.config._llmdv_stage == 'eligible':
        # Make the child kill the worker before Popen returns its pid to run_process.
        spawn = process.subprocess.Popen
        def slow_return(*args, **kwargs):
            child = spawn(*args, **kwargs)
            time.sleep(10)
            return child
        child = '''
import fcntl, os, signal, sys, time
with open(sys.argv[1], 'w') as file:
    fcntl.flock(file, fcntl.LOCK_EX)
    os.kill(os.getppid(), signal.SIGKILL)
    time.sleep(30)
'''
        argv = [sys.executable, '-c', child, os.environ['CLIENT_LOCK']]
        if os.environ['CLIENT_GROUP'] == 'worker':
            subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            process.subprocess.Popen = slow_return
            process.run_process(argv, cwd=Path.cwd(), env={}, timeout=40)
""",
    )
    with FakeFleet() as fleet:
        path = profile(tmp_path, fleet, [ELIGIBLE[0]])
        result, _ = verify(
            tmp_path,
            path,
            2,
            "-p",
            "session_fault",
            env={**plugin, "CLIENT_LOCK": str(locked), "CLIENT_GROUP": client_group},
        )
    assert result.returncode == 4, result.stderr
    assert "no case results recorded" in result.stdout
    assert "nothing matched" not in result.stdout
    with locked.open() as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("ids", [["case", "case"], ["outside"]])
def test_unexpected_or_duplicate_evidence_rejects_completion(tmp_path, ids):
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps({"nodeid": node}) + "\n" for node in ids))
    with pytest.raises(RuntimeError, match="unexpected or duplicate"):
        concurrent._order(path, {"case": 0}, results=True)


def test_window_exposes_older_serial_conditions_without_changing_matching():
    legacy = {"case_id": "demo", "outcome": "pass"}
    current = {**legacy, "concurrency": 2, "stage": "eligible"}
    delta = compare_window([[legacy], [current]], [{**current, "outcome": "fail"}])
    assert len(delta.newly_failing) == 1
    assert delta.execution_changes == ["concurrency 1 (serial) → 2 (eligible)"]


def test_exclusive_case_retains_its_internal_fanout(tmp_path):
    # Hold the first three burst requests until all three have arrived. A scheduler
    # that limits HTTP requests instead of whole cases would deadlock at width two.
    burst = "scenarios.cancel_burst"
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    arrivals = 0

    @contextmanager
    def watch(case, path):
        nonlocal arrivals
        with lock:
            meet = case == burst and path == "/v1/chat/completions" and arrivals < 3
            if meet:
                arrivals += 1
        if meet:
            barrier.wait(timeout=20)
        yield

    with FakeFleet(request_window=watch) as fleet:
        path = profile(tmp_path, fleet, [ELIGIBLE[0], burst])
        result, run = verify(tmp_path, path, 2, "--surface", "chat_completions", "--skip-preflight")
    assert result.returncode == 0, result.stderr
    [record] = [r for r in rows(run) if r["case_id"] == burst]
    assert (record["stage"], record["outcome"], arrivals) == ("exclusive", "pass", 3)
    log_lines = (run / "llmdv.log").read_text().splitlines()
    assert any("httpx" in line and record["nodeid"] in line for line in log_lines)
    assert (run / "results.jsonl").stat().st_mode & 0o777 == (
        run / "llmdv.log"
    ).stat().st_mode & 0o777
