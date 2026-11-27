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


@pytest.fixture(autouse=True)
def reset_deadline(monkeypatch):
    monkeypatch.setattr(smoke, "COMMAND_DEADLINE", None)


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


def test_staged_cancellation_leaves_resources_for_recovery(step, monkeypatch):
    def setup():
        step.created = True
        step.checkpoint()
        step.interrupt(15, None)

    monkeypatch.setattr(step, "setup", setup)
    monkeypatch.setattr(
        step, "finish", lambda: pytest.fail("cancelled preparation must not clean up")
    )
    assert step.staged("prepare") == 0
    recovered = smoke.SmokeStep(step.env)
    recovered.restore()
    assert recovered.pod == step.pod
    assert recovered.created
    assert recovered.status == "interrupted"
    calls = []
    monkeypatch.setattr(recovered, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(recovered, "collect", lambda: calls.append("collect"))
    monkeypatch.setattr(recovered, "kube", lambda *a, **k: calls.append(a[0]))
    assert recovered.staged("finalize") == 0
    assert calls == ["stop", "collect", "delete", "delete"]
    assert recovered.cancellation_recovery
    assert recovered.grace <= 20
    assert smoke.COMMAND_DEADLINE - smoke.time.monotonic() <= 20


def test_saved_verdict_survives_failing_cleanup(step, monkeypatch):
    step.created = True
    step.rc = 1
    step.checkpoint()
    recovered = smoke.SmokeStep(step.env)
    monkeypatch.setattr(recovered, "collect", lambda: setattr(recovered, "collection", "failed"))

    def fail(*a, **k):
        raise smoke.IntegrationError("cleanup unavailable")

    monkeypatch.setattr(recovered, "kube", fail)
    assert recovered.staged("finalize") == 1
    saved = json.loads((recovered.runs / "integration.json").read_text())
    assert saved["verification_exit"] == 1
    assert saved["collection_status"] == "failed"
    assert len(saved["warnings"]) == 2


def test_real_signal_to_verify_process_exits_promptly_and_saves_state(step, tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time

    step.ready = step.created = True
    step.status = "prepared"
    step.checkpoint()
    binary = tmp_path / "bin"
    binary.mkdir()
    ready = tmp_path / "command-ready"
    fake = binary / "kubectl"
    fake.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nimport time\n"
        f"Path({str(ready)!r}).touch()\ntime.sleep(60)\n"
    )
    fake.chmod(0o755)
    process = subprocess.Popen(
        [sys.executable, str(PATH), "verify"],
        env={**os.environ, **step.env, "PATH": str(binary)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        started = time.monotonic()
        process.send_signal(signal.SIGINT)
        _, error = process.communicate(timeout=3)
        assert process.returncode == 0, error
        assert time.monotonic() - started < 3
        recovered = smoke.SmokeStep(step.env)
        recovered.restore()
        assert recovered.status == "interrupted"
        assert recovered.created and recovered.pod == step.pod
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_profile_inheritance_stays_in_checkout(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    root.mkdir()
    leaf = root / "leaf.yaml"
    parent = root / "parent.yaml"
    leaf.write_text("leaf")
    parent.write_text("parent")
    seen = []

    def read(argv):
        path = Path(argv[-1])
        seen.append(path)
        return json.dumps({"extends": "parent.yaml"} if path == leaf else {})

    monkeypatch.setattr(smoke, "command", read)
    smoke.validate_inheritance(root, leaf)
    assert seen == [leaf, parent]
    outside = tmp_path / "outside.yaml"
    outside.write_text("private")
    parent.unlink()
    parent.symlink_to(outside)
    seen.clear()
    with pytest.raises(smoke.IntegrationError, match="within"):
        smoke.validate_inheritance(root, leaf)
    assert seen == [leaf]  # Refused before reading outside content.


@pytest.mark.parametrize("binding", ["valid", "inline", "other_env"])
def test_explicit_profile_uses_runtime_validation_and_no_mapped_identity(
    step, tmp_path, monkeypatch, binding
):
    root = tmp_path / "source"
    root.mkdir()
    (root / "custom.yaml").write_text("{}")
    step.env.update(
        LLMDV_PROFILE_ROOT=str(root),
        LLMDV_PROFILE="custom.yaml",
        LLMDV_PROFILE_REF="my-branch",
        LLMDV_PROFILE_REPOSITORY="example/profiles",
    )
    monkeypatch.setattr(smoke, "lookup", lambda _: pytest.fail("explicit profile must bypass map"))
    monkeypatch.setattr(step, "runner", lambda: ["pinned-runtime"])
    commands = []

    def command(argv, **kwargs):
        commands.append(argv)
        if argv[0] == "yq":
            return (
                json.dumps(
                    {
                        "ingresses": [
                            {
                                "id": "router",
                                "kind": "router",
                                "base_url_env": "OTHER_URL"
                                if binding == "other_env"
                                else "LLMDV_ROUTER_URL",
                                **(
                                    {"base_url": "http://unrelated.invalid"}
                                    if binding == "inline"
                                    else {}
                                ),
                            }
                        ]
                    }
                )
                if Path(argv[-1]).name == "profile.yaml"
                else "{}"
            )
        if argv[0] == "git":
            return "a" * 40
        if "show" in argv:
            return "ingresses: []"
        if "plan" in argv:
            assert kwargs["env"]["LLMDV_ROUTER_URL"].startswith("http://")
        return ""

    monkeypatch.setattr(smoke, "command", command)
    pods = []

    def kube(*args, **kwargs):
        if args[0] == "get":
            return '{"spec":{"ports":[{"port":80}]}}'
        if args[0] == "apply":
            assert json.loads(step.state.read_text())["created"]
            pods.append(json.loads(kwargs["input"]))
        return ""

    monkeypatch.setattr(step, "kube", kube)
    assert step.staged("prepare") == 0
    if binding != "valid":
        assert step.status == "setup_error"
        assert not pods and not step.created
        return
    assert step.status == "prepared"
    argv = json.loads(pods[0]["spec"]["containers"][0]["env"][1]["value"])
    assert "--identity" not in argv
    assert argv[argv.index("--ingress") + 1] == "router"
    assert any("plan" in cmd for cmd in commands)
    source = json.loads((step.runs / "profile-source.json").read_text())
    assert source["commit"] == "a" * 40
    assert source["path"] == "custom.yaml"
    assert source["selection"] == "explicit"


def test_incompatible_profile_is_rejected_before_creating_resources(step, tmp_path, monkeypatch):
    (tmp_path / "custom.yaml").write_text("{}")
    step.env.update(LLMDV_PROFILE_ROOT=str(tmp_path), LLMDV_PROFILE="custom.yaml")
    monkeypatch.setattr(step, "runner", lambda: ["pinned-runtime"])

    def command(argv, **kwargs):
        if "show" in argv:
            raise smoke.IntegrationError("unsupported profile capability")
        return "{}"

    monkeypatch.setattr(smoke, "command", command)
    calls = []

    def kube(*args, **kwargs):
        calls.append(args[0])
        return '{"spec":{"ports":[{"port":80}]}}'

    monkeypatch.setattr(step, "kube", kube)
    assert step.staged("prepare") == 0
    assert step.status == "setup_error"
    assert not step.created
    assert calls == ["get"]


@pytest.mark.parametrize(
    "raw,expected", [("-9", 4), ("137", 4), ("42", 4), ("-15", 6), ("1", 1), ("broken", 4)]
)
def test_abnormal_exit_is_recorded_without_waiting_for_timeout(step, monkeypatch, raw, expected):
    calls = []

    def kube(*args, **kwargs):
        calls.append(args)
        return raw

    monkeypatch.setattr(step, "kube", kube)
    assert step.wait(30)
    assert step.rc == expected
    assert len(calls) == 1
    restored = smoke.SmokeStep(step.env)
    restored.restore()
    assert restored.rc == expected


def test_dead_container_is_not_mistaken_for_running_verification(step, monkeypatch):
    def kube(*args, **kwargs):
        if args[0] == "exec":
            raise smoke.IntegrationError("container is stopped")
        return json.dumps(
            {
                "status": {
                    "containerStatuses": [
                        {
                            "name": "llmdv",
                            "state": {"terminated": {"exitCode": 137, "reason": "OOMKilled"}},
                        }
                    ]
                }
            }
        )

    monkeypatch.setattr(step, "kube", kube)
    assert step.wait(30)
    assert step.rc == 4
    assert any("OOMKilled" in message for message in step.warnings)


def test_cleanup_reserves_time_for_configmap_after_slow_pod_delete(step, monkeypatch):
    step.phase = "finalize"
    step.cancellation_recovery = True
    step.created = True
    now = [100.0]
    monkeypatch.setattr(smoke.time, "monotonic", lambda: now[0])
    calls = []

    def kube(*args, timeout, **kwargs):
        calls.append((args[1], timeout))
        now[0] += timeout
        raise smoke.IntegrationError("request timed out")

    monkeypatch.setattr(step, "kube", kube)
    step.finish()
    assert [kind for kind, _ in calls] == ["pod", "configmap"]
    assert calls[0][1] == 10
    assert calls[1][1] == 10


def test_legacy_setup_failure_collects_before_deleting_created_resources(step, monkeypatch):
    order = []

    def setup():
        step.created = True
        raise smoke.IntegrationError("readiness failed after pod creation")

    monkeypatch.setattr(step, "setup", setup)
    monkeypatch.setattr(step, "stop", lambda: order.append("stop"))
    monkeypatch.setattr(step, "collect", lambda: order.append("collect"))
    monkeypatch.setattr(step, "kube", lambda *args, **kw: order.append(args[0]))
    assert step.run() == 0
    assert order == ["stop", "collect", "delete", "delete"]


@pytest.mark.parametrize("broken", ["outputs", "summary", "report", "state", "all"])
def test_reporting_failures_do_not_prevent_recovery(step, monkeypatch, broken):
    step.created = True
    step.ready = True
    step.rc = 1
    step.status = "completed"
    step.checkpoint()
    if broken in ("outputs", "all"):
        step.env["GITHUB_OUTPUT"] = str(step.runs)
    if broken in ("summary", "all"):
        step.env["GITHUB_STEP_SUMMARY"] = str(step.runs)
    if broken in ("report", "all"):
        (step.runs / "integration.tmp").mkdir()
    if broken in ("state", "all"):
        step.state.with_suffix(".tmp").mkdir()
    recovered = smoke.SmokeStep(step.env)
    calls = []

    def kube(*args, **kwargs):
        calls.append(args)
        if args[0] == "cp":
            evidence(Path(args[-1]))
        return "console"

    monkeypatch.setattr(recovered, "kube", kube)
    assert recovered.staged("finalize") == 1
    assert recovered.rc == 1
    assert recovered.collection == "complete"
    assert [args[1] for args in calls if args[0] == "delete"] == ["pod", "configmap"]
    assert recovered.warnings
    if broken in ("report", "state"):
        assert "verification_exit=1" in Path(step.env["GITHUB_OUTPUT"]).read_text()


def test_console_write_failure_does_not_skip_evidence_copy(step, monkeypatch):
    (step.runs / "console.txt").mkdir()

    def kube(*args, **kwargs):
        if args[0] == "cp":
            evidence(Path(args[-1]))
        return "console"

    monkeypatch.setattr(step, "kube", kube)
    step.collect()
    assert step.collection == "complete"
    assert any("could not collect console" in note for note in step.warnings)


def test_initial_resource_identity_must_be_durable(step):
    step.state.with_suffix(".tmp").mkdir()
    with pytest.raises(smoke.IntegrationError, match="before creation"):
        step.checkpoint(required=True)
