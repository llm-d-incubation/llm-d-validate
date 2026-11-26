"""CI failures must not masquerade as either passing validation or a guide defect."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "deploy/llm-d-nightly/smoke_step.py"
spec = importlib.util.spec_from_file_location("nightly_smoke_step", PATH)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.fixture
def step(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke.time, "sleep", lambda _: None)
    return smoke.SmokeStep(
        {
            "RUNNER_TEMP": str(tmp_path),
            "LLMDV_VERSION": "0.1.2",
            "LLMDBENCH_CICD_SCENARIO": "optimized-baseline",
            "LLMDBENCH_CICD_NS": "test",
            "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        }
    )


def evidence(path, *, finished=True):
    run = path / "run"
    run.mkdir(parents=True)
    (run / "outcome.json").write_text(
        json.dumps(
            {"finished": "now" if finished else None, "verification": {"counts": {"pass": 1}}}
        )
    )
    (run / "results.jsonl").write_text('{"outcome":"pass"}\n')
    (run / "exchanges.jsonl").write_text("")
    for name in ("dashboard.html", "llmdv.log", "profile.used.yaml"):
        (run / name).write_text("evidence")


@pytest.mark.parametrize(
    "fields,key",
    [
        ({"ACCELERATOR": "gpu", "BACKEND": "vllm", "INFRA_PROVIDER": "gke"}, "gpu/vllm/gke"),
        ({"ACCELERATOR": "tpu/v5p", "BACKEND": "vllm", "INFRA_PROVIDER": ""}, "tpu/v5p/vllm"),
        (
            {
                "ACCELERATOR": "amd",
                "BACKEND": "vllm",
                "CONNECTOR": "lmcache-connector",
                "OFFLOADING_TARGET": "cpu",
                "INFRA_PROVIDER": "amd-ci",
            },
            "amd/vllm/lmcache-connector/cpu/amd-ci",
        ),
        (
            {"ACCELERATOR": "gpu", "BACKEND": "vllm", "INFRA_PROVIDER": "gke/base"},
            "gpu/vllm/gke/base",
        ),
    ],
)
def test_lookup_uses_effective_overlay_inputs(monkeypatch, fields, key):
    row = {"profile": "test", "identity": ["provider=base"]}
    monkeypatch.setattr(smoke, "command", lambda _: json.dumps({"lanes": {"guide": {key: row}}}))
    env = {f"LLMDBENCH_CICD_{k}": v for k, v in fields.items()}
    env["LLMDBENCH_CICD_SCENARIO"] = "guide"
    assert smoke.lookup(env) == row


@pytest.mark.parametrize("failure", [smoke.IntegrationError("yq missing"), ValueError("bad map")])
def test_lookup_failure_is_setup_error_not_skip(step, monkeypatch, failure):
    def broken(_):
        raise failure

    monkeypatch.setattr(smoke, "lookup", broken)
    assert step.run() == 0
    status = json.loads((step.runs / "integration.json").read_text())
    assert status["status"] == "setup_error"
    assert status["verification_exit"] is None
    assert status["warnings"]


def test_unmapped_overlay_is_an_explicit_skip(step, monkeypatch):
    monkeypatch.setattr(smoke, "lookup", lambda _: None)
    assert step.run() == 0
    assert step.status == "skipped"
    assert not step.warnings


def test_runner_ignores_path_installation_and_pins_version(step, monkeypatch):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        return "0.1.2" if "-c" in argv else ""

    monkeypatch.setattr(smoke, "command", command)
    argv = step.runner()
    assert argv[0] == str(step.runs / ".runner-venv/bin/python")
    assert any("llm-d-validate==0.1.2" in call for call in calls)
    assert all(call[0] != "llmdv" for call in calls)


def test_runner_refuses_mismatched_test_wheel(step, monkeypatch):
    monkeypatch.setattr(smoke, "command", lambda *a, **k: "0.1.1")
    with pytest.raises(smoke.IntegrationError, match="version differs"):
        step.runner()


@pytest.mark.parametrize("failures,expected", [(1, "complete"), (3, "failed")])
def test_copy_retries_and_reports_actual_delivery(step, monkeypatch, failures, expected):
    attempts = []

    def kube(*args, **kwargs):
        if args[0] == "cp":
            attempts.append(args)
            if len(attempts) <= failures:
                raise smoke.IntegrationError("copy lost")
            evidence(Path(args[-1]))
        return "console"

    monkeypatch.setattr(step, "kube", kube)
    step.collect()
    step.finish()
    assert step.collection == expected
    assert len(attempts) == min(failures + 1, 3)
    assert (step.runs / "runs").exists() == (expected == "complete")
    assert json.loads((step.runs / "integration.json").read_text())["collection_status"] == expected


@pytest.mark.parametrize(
    "damage", ["unfinished", "partial_line", "wrong_counts", "missing_dashboard"]
)
def test_collection_rejects_unfinalized_or_inconsistent_results(tmp_path, damage):
    evidence(tmp_path, finished=damage != "unfinished")
    if damage == "partial_line":
        (tmp_path / "run/results.jsonl").write_text('{"outcome":"pass"}')
    elif damage == "wrong_counts":
        (tmp_path / "run/results.jsonl").write_text("")
    elif damage == "missing_dashboard":
        (tmp_path / "run/dashboard.html").unlink()
    with pytest.raises(smoke.IntegrationError):
        smoke.validate_collection(tmp_path)


@pytest.mark.parametrize("cancelled", [False, True])
def test_timeout_or_cancellation_finalizes_before_collecting(step, monkeypatch, cancelled):
    order = []
    step.cancelled = cancelled
    monkeypatch.setattr(step, "setup", lambda: True)
    monkeypatch.setattr(step, "wait", lambda *a, **k: False)

    def stop():
        order.append("stop")
        step.rc = 6

    monkeypatch.setattr(step, "stop", stop)
    monkeypatch.setattr(step, "collect", lambda: order.append("collect"))
    assert step.run() == 0
    assert order == ["stop", "collect"]
    assert step.status == ("interrupted" if cancelled else "timed_out")


@pytest.mark.parametrize("code", range(7))
def test_only_recorded_fleet_failure_blocks_even_if_delivery_fails(step, monkeypatch, code):
    monkeypatch.setattr(step, "setup", lambda: True)

    def wait(_):
        step.rc = code
        return True

    monkeypatch.setattr(step, "wait", wait)

    def collect():
        step.collection = "failed"

    monkeypatch.setattr(step, "collect", collect)
    assert step.run() == (1 if code == 1 else 0)
    assert f"verification_exit={code}\n" in Path(step.env["GITHUB_OUTPUT"]).read_text()


def test_kubectl_failure_is_bounded_and_preserves_diagnostics(monkeypatch):
    def timeout(*a, **k):
        assert k["timeout"] == 7
        raise smoke.subprocess.TimeoutExpired(a[0], 7)

    monkeypatch.setattr(smoke.subprocess, "run", timeout)
    with pytest.raises(smoke.IntegrationError, match="TimeoutExpired"):
        smoke.command(["kubectl", "get", "pods"], timeout=7)


def test_signal_interrupts_setup_and_collects_before_cleanup(step, monkeypatch):
    order = []

    def setup():
        step.created = True
        step.interrupt(15, None)

    monkeypatch.setattr(step, "setup", setup)
    monkeypatch.setattr(step, "stop", lambda: order.append("stop"))
    monkeypatch.setattr(step, "collect", lambda: order.append("collect"))
    monkeypatch.setattr(step, "kube", lambda *a, **k: order.append("delete"))
    assert step.run() == 0
    assert step.status == "interrupted"
    assert order == ["stop", "collect", "delete", "delete"]


def test_pod_runner_forwards_stop_and_preserves_exit_before_collection(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time

    child_code = (
        "import signal,time,sys; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(6)); "
        f"Path({str(tmp_path / 'ready')!r}).write_text('ready'); time.sleep(60)"
    )
    runner = PATH.with_name("pod_runner.py")
    code = (
        "import runpy; from pathlib import Path; "
        f"runpy.run_path({str(runner)!r})['main'](Path({str(tmp_path)!r}))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        env={**os.environ, "LLMDV_COMMAND": json.dumps([sys.executable, "-c", child_code])},
    )

    def wait_for(path):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert path.exists()

    try:
        wait_for(tmp_path / "ready")
        process.send_signal(signal.SIGTERM)
        wait_for(tmp_path / "exit")
        assert (tmp_path / "exit").read_text() == "6"
        assert process.poll() is None  # Files stay available for collection.
        process.terminate()
        assert process.wait(timeout=5) == 0  # Deletion does not wait for forced termination.
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_timeout_racing_completion_does_not_signal_reaped_pid(step, monkeypatch):
    def wait(*args, **kwargs):
        step.rc = 1
        return True

    monkeypatch.setattr(step, "wait", wait)

    def unexpected(*args, **kwargs):
        pytest.fail("must not signal a completed verification process")

    monkeypatch.setattr(step, "kube", unexpected)
    step.stop()
    assert step.rc == 1
