"""Pod-log collection and run-level reproduction.

Both exist for the same reason: a result nobody can re-derive is a claim. The log collector
puts each layer's own account next to the HTTP evidence; reproduce.sh re-runs the whole
verification against the declarations it was actually made with.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from llmdv.cli import banner, verify_cmd
from llmdv.collect import KubectlLogCollector, NoopCollector
from llmdv.evidence import reproduce
from llmdv.evidence.outcome import Outcome
from llmdv.kube import READ_VERBS, KubeContext, KubeError
from llmdv.output import Offloader
from llmdv.preflight import Report
from llmdv.profile import loader

PROFILE = """
schema: llmdv.profile/1
name: logs
environment: {namespace: ns}
model: {public_id: m}
ingresses:
  - id: vllm
    kind: vllm
    base_url: http://localhost:1
    surfaces: [chat_completions]
    setup:
      requires:
        - kind: port_forward
          selector: role=decode
          local_port: 1
          remote_port: 8000
  - id: router
    kind: llmd_router
    base_url: http://localhost:2
    surfaces: [chat_completions]
    logs:
      - {target: svc/epp, container: epp, tail: 50}
"""


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text(textwrap.dedent(PROFILE))
    return loader.load(path)


# ------------------------------------------------------------------ log sources


def test_log_sources_fall_back_to_the_port_forward_target(profile):
    """The common case needs no extra configuration: the forward already names the pod."""
    sources = profile.ingress("vllm").log_sources
    assert len(sources) == 1
    assert sources[0].selector == "role=decode"


def test_declared_log_sources_win_over_the_fallback(profile):
    sources = profile.ingress("router").log_sources
    assert len(sources) == 1
    assert sources[0].target == "svc/epp"
    assert sources[0].container == "epp"
    assert sources[0].tail == 50


# ------------------------------------------------------------------ the guard


def test_logs_is_a_read_verb_and_mutation_is_still_refused():
    """Reading a container's stdout changes nothing; the allowlist still blocks writes."""
    assert "logs" in READ_VERBS
    for verb in ("apply", "delete", "exec", "scale", "patch"):
        with pytest.raises(KubeError, match="read-only"):
            KubeContext()._run(verb, "thing")


# ------------------------------------------------------------------ collection


class FakeKube:
    """Stands in for a cluster. Records what was asked for, so the windowing is checkable."""

    available = True

    def __init__(self):
        self.calls: list[dict] = []

    def pods_for_selector(self, selector, namespace=None):
        return ["pod-a", "pod-b"] if selector == "role=decode" else ["epp-0"]

    def service_selector(self, name, namespace=None):
        return "app=epp" if name == "epp" else None

    def containers_in(self, pod, namespace=None):
        return ["modelserver"]

    def pod_logs(self, pod, container=None, namespace=None, since_time=None, tail=2000):
        self.calls.append(
            {
                "pod": pod,
                "container": container,
                "namespace": namespace,
                "since_time": since_time,
                "tail": tail,
            }
        )
        return f"line from {pod}/{container}\nsecond line\n"


def test_collection_writes_one_file_per_container(profile, tmp_path):
    kube = FakeKube()
    collection = KubectlLogCollector(kube, "ns").collect(
        profile, ["vllm", "router"], "2026-10-01T02:00:00Z", tmp_path / "logs"
    )
    assert len(collection.ok) == 3  # two decode pods + one epp
    for entry in collection.ok:
        assert (tmp_path / entry.path.replace("logs/", "logs/")).is_file()
    assert {c["pod"] for c in kube.calls} == {"pod-a", "pod-b", "epp-0"}


def test_collection_is_windowed_to_the_run(profile, tmp_path):
    """Unwindowed logs from a pod up for days would bury the lines that belong to this run."""
    kube = FakeKube()
    KubectlLogCollector(kube, "ns").collect(
        profile, ["vllm"], "2026-10-01T02:00:00Z", tmp_path / "logs"
    )
    assert all(call["since_time"] == "2026-10-01T02:00:00Z" for call in kube.calls)


def test_declared_tail_is_honoured(profile, tmp_path):
    kube = FakeKube()
    KubectlLogCollector(kube, "ns").collect(profile, ["router"], None, tmp_path / "logs")
    assert kube.calls[0]["tail"] == 50
    assert kube.calls[0]["container"] == "epp"


def test_offloaded_logs_fetch_whole_but_keep_declared_tail_locally(profile, tmp_path):
    class Long(FakeKube):
        def pod_logs(self, pod, **kwargs):
            self.calls.append(kwargs)
            return "".join(f"line {n}\n" for n in range(120))

    sink = tmp_path / "sink"
    kube = Long()
    collection = KubectlLogCollector(
        kube, "ns", Offloader(f"file://{sink}", "profile/run")
    ).collect(profile, ["router"], None, tmp_path / "logs")

    entry = collection.ok[0]
    assert kube.calls[0]["tail"] == -1
    local = (tmp_path / entry.path).read_text().splitlines()
    assert local == [f"line {n}" for n in range(70, 120)]
    assert entry.truncated is True
    stored = next(sink.rglob("*.log"))
    assert len(stored.read_text().splitlines()) == 120
    assert entry.to_dict()["artifact"]["offload"]["status"] == "stored"


def test_small_offloaded_log_stays_local(profile, tmp_path):
    class Short(FakeKube):
        def pod_logs(self, pod, **kwargs):
            self.calls.append(kwargs)
            return "".join(f"line {n}\n" for n in range(30))

    sink = tmp_path / "sink"
    collection = KubectlLogCollector(
        Short(), "ns", Offloader(f"file://{sink}", "profile/run")
    ).collect(profile, ["router"], None, tmp_path / "logs")

    assert collection.ok[0].truncated is False
    assert collection.ok[0].artifact is None
    assert not sink.exists()


def test_whole_log_failure_falls_back_to_declared_tail(profile, tmp_path):
    class Fallback(FakeKube):
        def pod_logs(self, pod, **kwargs):
            self.calls.append(kwargs)
            if kwargs["tail"] == -1:
                raise KubeError("too large")
            return "".join(f"line {n}\n" for n in range(50))

    kube = Fallback()
    collection = KubectlLogCollector(
        kube, "ns", Offloader(f"file://{tmp_path / 'sink'}", "profile/run")
    ).collect(profile, ["router"], None, tmp_path / "logs")

    assert [call["tail"] for call in kube.calls] == [-1, 50]
    assert collection.ok[0].artifact["offload"]["status"] == "failed"
    assert collection.ok[0].artifact["bytes"] is None
    assert collection.ok[0].artifact["sha256"] is None
    assert collection.ok[0].artifact["kept_bytes"] == len(
        (tmp_path / collection.ok[0].path).read_bytes()
    )
    args = SimpleNamespace(
        profile=tmp_path / "profile.yaml", ingress=[], artifact_sink="file:///sink"
    )
    verify_cmd._write_run_metadata(tmp_path, profile, args, {}, Outcome("run"), collection)
    metadata = json.loads((tmp_path / "env.json").read_text())
    assert metadata["pod_logs"]["offloaded"] == 0


def test_collected_logs_are_redacted(profile, tmp_path, monkeypatch):
    from llmdv.http import redact

    redact.reset_secrets()
    redact.install_secrets(["sk-leaked-value"])

    class Leaky(FakeKube):
        def pod_logs(self, pod, container=None, namespace=None, since_time=None, tail=2000):
            return "starting with key sk-leaked-value\n"

    collection = KubectlLogCollector(Leaky(), "ns").collect(
        profile, ["router"], None, tmp_path / "logs"
    )
    body = (tmp_path / "logs").joinpath(Path(collection.ok[0].path).name).read_text()
    assert "sk-leaked-value" not in body
    assert redact.SENTINEL in body
    redact.reset_secrets()


def test_a_failing_pod_is_recorded_rather_than_raising(profile, tmp_path):
    class Broken(FakeKube):
        def pod_logs(self, *a, **kw):
            raise KubeError("pod not found")

    collection = KubectlLogCollector(Broken(), "ns").collect(
        profile, ["router"], None, tmp_path / "logs"
    )
    assert collection.ok == []
    assert collection.failed and "pod not found" in collection.failed[0].error
    assert collection.reason == "all pod log reads failed"


@pytest.mark.parametrize("failed_pods", [{"pod-a", "pod-b"}, {"pod-a"}], ids=["all", "partial"])
def test_failed_collection_keeps_errors_and_reports_only_saved_logs(
    profile, tmp_path, monkeypatch, failed_pods
):
    class Broken(FakeKube):
        def pod_logs(self, pod, **kwargs):
            if pod in failed_pods:
                raise KubeError("Forbidden: cannot read pods/log")
            return super().pod_logs(pod, **kwargs)

    args = SimpleNamespace(pod_logs="all", profile=tmp_path / "p.yaml", ingress=["vllm"])
    records = [{"case_id": "smoke", "ingress": "vllm", "outcome": "pass"}]
    collection = verify_cmd._collect_logs(
        args, profile, ["vllm"], Broken(), tmp_path, None, records, Report()
    )
    verify_cmd._summarise(tmp_path, records, collection)
    verify_cmd._write_run_metadata(tmp_path, profile, args, {}, Outcome("logs"), collection)
    output = []
    monkeypatch.setattr(banner, "emit", output.append)
    banner.finish(tmp_path, records, [], 0, collected=collection)

    index = json.loads((tmp_path / "logs/index.json").read_text())
    assert {entry["pod"] for entry in index if entry["error"]} == failed_pods
    for entry in index:
        if entry["pod"] in failed_pods:
            assert entry["path"] is None
            assert "Forbidden" in entry["error"]
        else:
            assert (tmp_path / entry["path"]).is_file()
    pod_logs = json.loads((tmp_path / "env.json").read_text())["pod_logs"]
    assert pod_logs["collected"] == 2 - len(failed_pods)
    summary = (tmp_path / "summary.md").read_text()
    if len(failed_pods) == 2:
        assert pod_logs["reason"] == "all pod log reads failed"
        assert "None were collected: all pod log reads failed" in summary
        assert "Correlating the pod logs" not in summary
        assert "none: all pod log reads failed" in "\n".join(output)
    else:
        assert pod_logs["reason"] is None
        assert "Correlating the pod logs" in summary
        assert "None were collected" not in summary


def test_noop_collector_returns_nothing_but_says_why(profile, tmp_path):
    """The reason is the point: an empty Collection alone cannot be read honestly later."""
    out = tmp_path / "logs"
    collection = NoopCollector("kubectl is not on PATH").collect(profile, ["vllm"], None, out)
    assert collection.logs == []
    assert collection.reason == "kubectl is not on PATH"
    assert not out.exists()


# ------------------------------------------------------------------ reproduce.sh


def test_rewrite_argv_repoints_the_profile_and_drops_the_output_dir():
    """A rerun must not overwrite the evidence it is reproducing."""
    out = reproduce.rewrite_argv(
        [
            "llmdv",
            "verify",
            "--profile",
            "profiles/x.yaml",
            "--local-output",
            "/tmp/r",
            "--ingress",
            "vllm",
        ],
        "profile.used.yaml",
    )
    assert out == ["llmdv", "verify", "--profile", "profile.used.yaml", "--ingress", "vllm"]


def test_rewrite_argv_handles_the_equals_form():
    out = reproduce.rewrite_argv(["llmdv", "verify", "--local-output=/tmp/r", "-q"], "p.yaml")
    assert out == ["llmdv", "verify", "-q"]


def test_script_is_runnable_and_carries_no_credential(tmp_path):
    path = reproduce.write(
        tmp_path,
        profile_name="fleet",
        profile_digest="sha256:abc123",
        argv=["llmdv", "verify", "--profile", "profile.used.yaml"],
        setup_commands={"vllm": ["kubectl -n ns port-forward pod/x 1:2"]},
        key_envs=["LLMDV_LITELLM_KEY"],
    )
    text = path.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert path.stat().st_mode & 0o111, "script is not executable"
    assert "sha256:abc123" in text
    assert "LLMDV_LITELLM_KEY:?set" in text  # read from the environment, not embedded
    assert "port-forward pod/x 1:2" in text
    assert "LLMDV_RERUN_OUT" in text  # fresh output directory
    assert "--profile profile.used.yaml" in text
