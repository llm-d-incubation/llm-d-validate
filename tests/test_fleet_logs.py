"""Log-signature cases against the kubectl double: each forbidden line fails its own source.

The double serves windowed container logs from its state and records every invocation, so the
tests also prove the window is passed and that only read verbs are used.
"""

from __future__ import annotations

import pytest

from llmdv.kube import READ_VERBS
from llmdv.plugin import PHASE_ORDER
from llmdv.profile import loader
from tests.fleet_fakes import (
    LOG_DEFECTS,
    LOG_ENVIRONMENT_YAML,
    LOG_VARIANTS,
    LOGS_INGRESS_YAML,
    SEEDED_REQUEST_ID,
    crash_and_restart,
    fake_kubectl,
    fleet_profile,
    fleet_state,
    kubectl_calls,
    no_kubectl_env,
    run_fleet_suite,
    seed_exchange,
)

CASE = "logs.signatures"


def run(
    tmp_path, defects=(), env=None, ingress_extra=LOGS_INGRESS_YAML, state=None, started_at=True
):
    profile = fleet_profile(
        tmp_path / "profile.yaml", LOG_ENVIRONMENT_YAML, ingress_extra=ingress_extra
    )
    env = env if env is not None else fake_kubectl(tmp_path / "bin", state or fleet_state(defects))
    seed_exchange(tmp_path / "run")
    window = ("--llmdv-started-at", "2026-01-01T00:00:00Z") if started_at else ()
    result, records = run_fleet_suite(profile, tmp_path / "run", env, *window, "-k", "logs")
    return result, [r for r in records if r.get("case_id") == CASE], env


def test_clean_logs_pass_every_source(tmp_path):
    result, records, env = run(tmp_path)
    assert {r["variant"]: r["outcome"] for r in records} == {
        "app=router/router": "pass",
        "app=engine/vllm": "pass",
    }, result.stdout[-3000:]
    assert {r["ingress"] for r in records} == {"fake"}
    calls = kubectl_calls(env)
    verbs = {next(word for word in call if not word.startswith("-")) for call in calls}
    assert verbs <= READ_VERBS
    log_calls = [call for call in calls if "logs" in call]
    assert log_calls and all(any(w.startswith("--since-time=") for w in c) for c in log_calls)
    assert (tmp_path / "run" / "log-scan" / "fake__engine-0__vllm.log").is_file()


def test_a_direct_pytest_run_still_opens_a_log_window(tmp_path):
    _, _, env = run(tmp_path, started_at=False)
    log_calls = [call for call in kubectl_calls(env) if "logs" in call]
    assert log_calls and all(
        any(word.startswith("--since-time=") for word in call) for call in log_calls
    )


@pytest.mark.parametrize("defect", sorted(LOG_DEFECTS))
def test_each_forbidden_line_fails_its_source(tmp_path, defect):
    result, records, _ = run(tmp_path, (defect,))
    found = {r["variant"]: r for r in records}
    where, _, signature = LOG_DEFECTS[defect]
    target = LOG_VARIANTS[where]
    assert found[target]["outcome"] == "fail", (defect, result.stdout[-3000:])
    assert signature in found[target]["reason"]
    assert {r["outcome"] for v, r in found.items() if v != target} == {"pass"}


def test_an_allowed_class_excuses_a_matched_line(tmp_path):
    """The healthy router log carries a render failure from a client cancel, which is allowed."""
    _, records, _ = run(tmp_path)
    router = next(r for r in records if r["variant"] == "app=router/router")
    assert router["outcome"] == "pass"


def test_a_hit_is_tied_to_the_llmdv_case_that_sent_the_request(tmp_path):
    _, records, _ = run(tmp_path, ("validation_stack",))
    engine = next(r for r in records if r["variant"] == "app=engine/vllm")
    tied = next(c for c in engine["checks"] if c["name"] == "llmdv cases on hit lines")
    assert tied["observed"] == ["chat.seeded"]
    assert SEEDED_REQUEST_ID in str(engine["checks"])


def test_a_source_matching_no_pod_fails(tmp_path):
    state = fleet_state()
    for pod in state["objects"]["fleet"]["pods"]:
        pod["metadata"]["labels"] = {"app": "elsewhere"}
    _, records, _ = run(tmp_path, state=state)
    assert {r["outcome"] for r in records} == {"fail"}
    assert all("pods read" in r["reason"] for r in records)


def test_a_failed_read_fails(tmp_path):
    state = fleet_state()
    del state["logs"]["engine-1/vllm"]
    _, records, _ = run(tmp_path, state=state)
    engine = next(r for r in records if r["variant"] == "app=engine/vllm")
    assert engine["outcome"] == "fail" and "reads failed" in engine["reason"]


@pytest.mark.parametrize(
    "error",
    [
        'error: container "wrong" is not valid for pod "engine-1"',
        'container "vllm" in pod "engine-1" is waiting to start: CrashLoopBackOff',
        "kubectl timed out after 60s",
    ],
)
def test_a_non_access_log_error_fails(tmp_path, error):
    state = fleet_state()
    state["log_errors"] = {"engine-1/vllm": error}
    _, records, _ = run(tmp_path, state=state)
    engine = next(item for item in records if item["variant"] == "app=engine/vllm")
    assert engine["outcome"] == "fail" and "reads failed" in engine["reason"]


def test_a_forbidden_pod_list_is_missing_instrumentation(tmp_path):
    state = fleet_state()
    state["forbidden"] = ["pods"]
    _, records, _ = run(tmp_path, state=state)
    assert {(item["outcome"], item["skip_class"]) for item in records} == {
        ("skip", "requires_instrumentation")
    }


def test_no_kubectl_is_missing_instrumentation(tmp_path):
    _, records, _ = run(tmp_path, env=no_kubectl_env(tmp_path / "empty"))
    assert {(r["outcome"], r["skip_class"]) for r in records} == {
        ("skip", "requires_instrumentation")
    }


def test_without_signatures_one_undeclared_row(tmp_path):
    plain = "    logs:\n      - {selector: app=engine, container: vllm}\n"
    _, records, _ = run(tmp_path, ingress_extra=plain)
    assert [(r["outcome"], r["skip_class"], r["ingress"]) for r in records] == [
        ("skip", "undeclared", "fake")
    ]


def test_the_logs_phase_runs_after_every_other_phase():
    others = [rank for phase, rank in PHASE_ORDER.items() if phase != "logs"]
    assert PHASE_ORDER["logs"] > max(others + [len(PHASE_ORDER)])


@pytest.mark.parametrize(
    "logs, message",
    [
        ("forbid: [{id: a}]", "pattern"),
        ("forbid: [{id: a, pattern: '('}]", "invalid pattern"),
        ("forbid: [{id: a, pattern: x}, {id: a, pattern: y}]", "distinct"),
        ("forbid: [x]", "mapping"),
        ("allow: ['(']", "invalid pattern"),
    ],
)
def test_malformed_signatures_are_profile_errors(tmp_path, logs, message):
    extra = f"    logs:\n      - {{selector: app=engine, {logs}}}\n"
    with pytest.raises(loader.ProfileError, match=message):
        loader.load(fleet_profile(tmp_path / "p.yaml", ingress_extra=extra))


def test_a_crash_in_a_restarted_container_is_read_from_its_previous_instance(tmp_path):
    """The fatal line lives only in the instance before the restart, and still fails."""
    state = crash_and_restart(
        fleet_state(), "engine-1", "vllm", "ERROR EngineCore encountered a fatal error.\n"
    )
    _, records, env = run(tmp_path, state=state)
    engine = next(r for r in records if r["variant"] == "app=engine/vllm")
    assert engine["outcome"] == "fail" and "fatal" in engine["reason"]
    assert any("--previous" in call for call in kubectl_calls(env))


def test_a_restarted_sidecar_outside_the_source_is_not_scanned(tmp_path):
    state = fleet_state()
    engine = next(
        item for item in state["objects"]["fleet"]["pods"] if item["metadata"]["name"] == "engine-1"
    )
    engine["spec"]["containers"].append({"name": "sidecar"})
    engine["status"]["containerStatuses"] = [
        {
            "name": "sidecar",
            "restartCount": 1,
            "lastState": {"terminated": {"finishedAt": "2999-01-01T00:00:00Z"}},
        }
    ]
    state["logs"]["engine-1/sidecar#previous"] = "ERROR EngineCore encountered a fatal error.\n"
    _, records, env = run(tmp_path, state=state)
    engine_row = next(item for item in records if item["variant"] == "app=engine/vllm")
    assert engine_row["outcome"] == "pass"
    assert not any("sidecar" in call and "--previous" in call for call in kubectl_calls(env))


def test_a_failed_restart_status_read_is_missing_instrumentation(tmp_path):
    state = crash_and_restart(fleet_state(), "engine-1", "vllm", "INFO previous\n")
    state["forbidden_names"] = ["pods/engine-1"]
    _, records, _ = run(tmp_path, state=state)
    engine = next(item for item in records if item["variant"] == "app=engine/vllm")
    assert (engine["outcome"], engine["skip_class"]) == ("skip", "requires_instrumentation")


def test_the_scan_reads_every_pod_ready_or_not_and_every_line(tmp_path):
    state = fleet_state()
    pods = state["objects"]["fleet"]["pods"]
    for index in range(2, 8):
        pod = dict(pods[0], metadata={**pods[0]["metadata"], "name": f"engine-{index}"})
        pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "False"}]}
        pods.append(pod)
        state["logs"][f"engine-{index}/vllm"] = "INFO ok\n"
    state["logs"]["engine-7/vllm"] += "ERROR EngineCore encountered a fatal error.\n"
    _, records, env = run(tmp_path, state=state)
    engine = next(r for r in records if r["variant"] == "app=engine/vllm")
    assert engine["outcome"] == "fail"
    read = next(c for c in engine["checks"] if c["name"] == "pods read")["observed"]
    assert len(read) == 8
    log_calls = [call for call in kubectl_calls(env) if "logs" in call]
    assert all("--tail=-1" in call for call in log_calls)


def test_a_service_target_reads_the_unready_pods_behind_it(tmp_path):
    """A source named by its Service is scanned on every Running pod, so a standby is read too."""
    state = fleet_state()
    objects = state["objects"]["fleet"]
    active = next(pod for pod in objects["pods"] if pod["metadata"]["name"] == "router-a")
    standby = dict(active, metadata={**active["metadata"], "name": "router-b"})
    standby["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "False"}]}
    objects["pods"].append(standby)
    service = {"kind": "Service", "metadata": {"name": "router-svc"}}
    objects["service"] = [{**service, "spec": {"selector": {"app": "router"}}}]
    state["logs"]["router-b/router"] = "ERROR Failed to apply KV event batch\n"
    extra = (
        "    logs:\n      - target: svc/router-svc\n        container: router\n"
        "        forbid:\n          - {id: rejected, pattern: 'Failed to apply KV event batch'}\n"
    )
    _, records, _ = run(tmp_path, ingress_extra=extra, state=state)
    assert len(records) == 1 and records[0]["outcome"] == "fail", records
    read = next(c for c in records[0]["checks"] if c["name"] == "pods read")["observed"]
    assert read == ["router-a", "router-b"]


def test_a_missing_service_target_fails(tmp_path):
    extra = (
        "    logs:\n      - target: svc/no-such-service\n        container: router\n"
        "        forbid:\n          - {id: rejected, pattern: 'Failed'}\n"
    )
    _, records, _ = run(tmp_path, ingress_extra=extra)
    assert len(records) == 1
    assert records[0]["outcome"] == "fail" and "resolve log source" in records[0]["reason"]
