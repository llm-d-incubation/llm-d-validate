"""Cluster CLI selection and the evidence an oc-only run must still carry."""

from __future__ import annotations

import json
import shlex
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from llmdv import preflight
from llmdv.cli import banner, verify_cmd
from llmdv.cli.main import build_parser
from llmdv.collect import KubectlLogCollector
from llmdv.kube import NO_CLI_REASON, READ_VERBS, KubeContext, KubeError
from llmdv.profile import loader
from tests.fakes import FakeFleet, FakeSlack, profile_yaml, run_llmdv


def make_cli(tmp_path, name):
    """An executable double: every cluster request is recorded and answered locally."""
    binary = tmp_path / name
    calls = tmp_path / f"{name}.calls"
    pods = json.dumps(
        {"items": [{"metadata": {"name": "pod-a"},
                    "status": {"phase": "Running",
                               "conditions": [{"type": "Ready", "status": "True"}]}}]}
    )  # fmt: skip
    binary.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(calls))}\n'
        'case " $* " in\n'
        '  *" get --raw "*) echo "NotFound" >&2; exit 1 ;;\n'
        f'  *" get pods "*" -o json "*) printf "%s\\n" {shlex.quote(pods)} ;;\n'
        '  *" logs "*) printf "engine log\\n" ;;\n'
        '  *" get pod "*) printf "modelserver\\n" ;;\n'
        '  *" config current-context "*) printf "test-context\\n" ;;\n'
        '  *" get "*|*" version "*) printf "{}\\n" ;;\n'
        '  *) echo "unexpected command" >&2; exit 1 ;;\n'
        "esac\n"
    )
    binary.chmod(0o755)
    return binary, calls


@pytest.mark.parametrize(
    ("installed", "override", "expected"),
    [
        (["kubectl", "oc"], None, "kubectl"),
        (["oc"], None, "oc"),
        ([], None, None),
        (["kubectl", "oc"], "oc", "oc"),
        (["kubectl"], "oc", None),
        (["kubectl", "oc"], "", None),
    ],
)
def test_cli_selection(tmp_path, monkeypatch, installed, override, expected):
    for name in installed:
        make_cli(tmp_path, name)
    monkeypatch.setenv("PATH", str(tmp_path))
    kube = KubeContext(cli=override)
    assert kube.available is (expected is not None)
    assert kube.binary == (str(tmp_path / expected) if expected else None)
    if expected is None:
        reason = "requested cluster CLI" if override is not None else NO_CLI_REASON
        with pytest.raises(KubeError, match=reason):
            kube._run("get", "pods")


def test_selection_is_resolved_once_and_uses_the_resolved_path(tmp_path, monkeypatch):
    oc, calls = make_cli(tmp_path, "oc")
    monkeypatch.setenv("PATH", str(tmp_path))
    kube = KubeContext()
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("llmdv.kube.shutil.which", lambda name: pytest.fail("resolved twice"))
    assert kube.available
    assert kube.current_context() == "test-context"
    assert kube.binary == str(oc)
    assert "config current-context" in calls.read_text()


def test_explicit_path_and_cluster_coordinates_are_honoured(tmp_path, monkeypatch):
    oc, calls = make_cli(tmp_path, "custom oc")
    monkeypatch.setenv("PATH", "")
    kubeconfig = tmp_path / "config file"
    kube = KubeContext(kubeconfig=kubeconfig, context="chosen-context", cli=str(oc))
    assert kube.current_context() == "test-context"
    assert kube._base() == [str(oc), "--kubeconfig", str(kubeconfig), "--context", "chosen-context"]
    assert kube.display_prefix() == [str(oc), "--context", "chosen-context"]
    assert "--context chosen-context" in calls.read_text()


@pytest.mark.parametrize("cli", ["kubectl", "oc"])
@pytest.mark.parametrize("verb", ["apply", "delete", "exec", "patch", "new-project", "adm"])
def test_both_clis_refuse_mutation(tmp_path, monkeypatch, cli, verb):
    _, calls = make_cli(tmp_path, cli)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(KubeError, match="read-only"):
        KubeContext(cli=cli)._run(verb, "thing")
    assert not calls.exists()


@pytest.mark.parametrize("verb", sorted(READ_VERBS))
def test_oc_can_execute_each_read_verb(tmp_path, monkeypatch, verb):
    _, calls = make_cli(tmp_path, "oc")
    monkeypatch.setenv("PATH", str(tmp_path))
    rest = ["current-context"] if verb == "config" else ["thing"]
    assert KubeContext()._run(verb, *rest)
    assert calls.read_text().strip() == " ".join([verb, *rest])


@pytest.mark.parametrize("failure", ["timeout", "removed", "exit", "json"])
def test_errors_identify_the_chosen_cli(tmp_path, monkeypatch, failure):
    oc, _ = make_cli(tmp_path, "oc")
    monkeypatch.setenv("PATH", str(tmp_path))
    kube = KubeContext(namespace="ns")

    def run(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        if failure == "removed":
            raise FileNotFoundError("executable removed")
        return subprocess.CompletedProcess(args, 1 if failure == "exit" else 0, "", "")

    monkeypatch.setattr("llmdv.kube.subprocess.run", run)
    with pytest.raises(KubeError, match=str(oc)):
        kube.pods_for_selector("app=test")


@pytest.mark.parametrize(
    "argv",
    [
        ["--kube-cli", "oc", "verify", "--profile", "p.yaml"],
        ["verify", "--profile", "p.yaml", "--kube-cli", "oc"],
        ["--kube-cli", "oc", "profile", "show", "p.yaml", "--setup"],
        ["profile", "--kube-cli", "oc", "show", "p.yaml", "--setup"],
        ["profile", "show", "p.yaml", "--setup", "--kube-cli", "oc"],
    ],
)
def test_override_is_parsed_at_every_subcommand_level(argv):
    args, unknown = build_parser().parse_known_args(argv)
    assert args.kube_cli == "oc"
    assert not unknown


@pytest.mark.parametrize(
    ("both", "override", "chosen"),
    [(False, [], "oc"), (True, ["--kube-cli", "oc"], "oc"), (True, [], "kubectl")],
)
def test_verify_resolves_selectors_collects_logs_and_records_cli(tmp_path, both, override, chosen):
    make_cli(tmp_path, "oc")
    if both:
        make_cli(tmp_path, "kubectl")
    path = tmp_path / "profile.yaml"
    out = tmp_path / "run"
    with FakeFleet() as fleet:
        raw = yaml.safe_load(profile_yaml(fleet.base_url, fleet.model))
        raw["environment"] = {
            "namespace": "ns",
            "context": "ctx",
            "kubeconfig": str(tmp_path / "config file"),
        }
        raw["ingresses"][0]["setup"] = {
            "requires": [{"kind": "port_forward", "selector": "app=test",
                          "local_port": 18100, "remote_port": 8000}]
        }  # fmt: skip
        path.write_text(yaml.safe_dump(raw))
        result = run_llmdv(
            "verify", "--profile", str(path), "--local-output", str(out),
            "--pod-logs", "all", "-k", "models_lists", *override,
            env={"PATH": str(tmp_path)},
        )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert f"kube CLI  {chosen}" in result.stdout
    env = json.loads((out / "env.json").read_text())
    assert env["kube_cli"] == chosen
    assert env["kube_cli_requested"] == ("oc" if override else None)
    command = env["setup_commands"]["fake"][0]
    assert command.startswith(f"{chosen} --context ctx ")
    assert "--kubeconfig" not in command
    assert str(tmp_path) not in command
    assert "port-forward pod/pod-a" in command
    reproduction = (out / "reproduce.sh").read_text()
    assert command in reproduction
    assert "--kubeconfig" not in reproduction
    assert env["pod_logs"]["collected"] == 1
    assert (out / "logs/fake__pod-a__modelserver.log").read_text() == "engine log\n"
    calls = (tmp_path / f"{chosen}.calls").read_text()
    assert f"--kubeconfig {tmp_path / 'config file'} --context ctx" in calls
    assert "get pods -n ns" in calls
    assert "get --raw /api/v1/namespaces/ns/" in calls
    assert "logs pod-a --tail=" in calls
    if both:
        other = "kubectl" if chosen == "oc" else "oc"
        assert not (tmp_path / f"{other}.calls").exists()


def test_profile_setup_uses_oc_and_effective_coordinates(tmp_path):
    _, calls = make_cli(tmp_path, "oc")
    path = tmp_path / "profile.yaml"
    raw = yaml.safe_load(profile_yaml("http://localhost:1"))
    raw["environment"] = {"namespace": "ns", "context": "profile-context"}
    raw["ingresses"][0]["setup"] = {
        "requires": [{"kind": "port_forward", "selector": "app=test",
                      "local_port": 18100, "remote_port": 8000}]
    }  # fmt: skip
    path.write_text(yaml.safe_dump(raw))
    config = tmp_path / "config file"
    result = run_llmdv(
        "profile", "show", str(path), "--setup", "--kube-cli", "oc",
        "--context", "override-context", "--kubeconfig", str(config),
        env={"PATH": str(tmp_path)},
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "oc --context override-context" in result.stdout
    assert "--kubeconfig" not in result.stdout
    assert str(config) not in result.stdout
    assert f"--kubeconfig {config} --context override-context" in calls.read_text()
    assert "-n ns port-forward pod/pod-a 18100:8000" in result.stdout


@pytest.mark.parametrize("coordinate", ["namespace", "cluster", "context", "kubeconfig"])
@pytest.mark.parametrize("declared_logs", [False, True])
def test_missing_cli_warns_once_for_environment(tmp_path, monkeypatch, coordinate, declared_logs):
    path = tmp_path / "profile.yaml"
    raw = yaml.safe_load(profile_yaml("http://localhost:1"))
    raw["environment"] = {coordinate: "test-value"}
    if declared_logs:
        raw["ingresses"][0]["logs"] = [{"target": "pod/pod-a"}]
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(preflight, "_reachable", lambda ingress: (True, 1.0))
    # No fleet stands behind the stub, so its model list cannot be read either.
    monkeypatch.setattr(preflight, "_check_access", lambda report, ingress: None)
    report = preflight.run(loader.load(path), kube=KubeContext())
    assert report.ok
    assert len(report.warnings) == 1
    assert NO_CLI_REASON in report.warnings[0].message
    assert "selectors cannot be resolved" in report.warnings[0].message
    if declared_logs:
        assert report.log_access_reason == NO_CLI_REASON


def test_endpoint_only_profile_needs_no_cluster_cli(tmp_path, monkeypatch):
    path = tmp_path / "profile.yaml"
    path.write_text(profile_yaml("http://localhost:1"))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(preflight, "_reachable", lambda ingress: (True, 1.0))
    # No fleet stands behind the stub, so its model list cannot be read either.
    monkeypatch.setattr(preflight, "_check_access", lambda report, ingress: None)
    report = preflight.run(loader.load(path), kube=KubeContext())
    assert report.ok
    assert not report.warnings


def test_missing_cli_collector_explains_both_supported_binaries(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    out = tmp_path / "logs"
    collection = KubectlLogCollector(KubeContext()).collect(None, [], None, out)
    assert collection.reason == NO_CLI_REASON
    assert not out.exists()


@pytest.mark.parametrize("mode", ["failures", "all"])
@pytest.mark.parametrize("override", [False, True])
def test_unavailable_cli_is_reported_and_required_logs_still_fail(tmp_path, mode, override):
    path = tmp_path / "profile.yaml"
    out = tmp_path / "run"
    if override:
        make_cli(tmp_path, "kubectl")
    with FakeFleet() as fleet:
        raw = yaml.safe_load(profile_yaml(fleet.base_url, fleet.model))
        raw["environment"] = {"namespace": "ns"}
        raw["ingresses"][0]["logs"] = [{"target": "pod/pod-a"}]
        path.write_text(yaml.safe_dump(raw))
        result = run_llmdv(
            "verify", "--profile", str(path), "--local-output", str(out),
            "--pod-logs", mode, "-k", "models_lists",
            *(["--kube-cli", "oc"] if override else []),
            env={"PATH": str(tmp_path)},
        )  # fmt: skip
    assert result.returncode == (3 if mode == "all" else 0), result.stderr
    assert "kube CLI  unavailable" in result.stdout
    reason = "requested cluster CLI 'oc'" if override else NO_CLI_REASON
    assert reason in result.stderr
    assert "cluster lookups are skipped" in result.stderr
    assert result.stderr.count("warning: preflight:") == 1
    env = json.loads((out / "env.json").read_text())
    assert env["kube_cli"] is None
    assert env["kube_cli_requested"] == ("oc" if override else None)
    assert not (out / "logs").exists()
    assert not (tmp_path / "kubectl.calls").exists()


@pytest.mark.parametrize("cli", ["oc", ""])
def test_job_forwards_the_cli_override_to_verify(tmp_path, cli):
    make_cli(tmp_path, "kubectl")
    make_cli(tmp_path, "oc")
    path = tmp_path / "profile.yaml"
    work = tmp_path / "work"
    with FakeFleet() as fleet, FakeSlack() as slack:
        path.write_text(profile_yaml(fleet.base_url, fleet.model))
        result = run_llmdv(
            "job", "--profile", str(path), "--work-dir", str(work),
            "--kube-cli", cli, "--pod-logs", "off", "-k", "models_lists",
            env={"PATH": str(tmp_path), "LLMDV_NOTIFY_WEBHOOK": f"{slack.base_url}/services/x"},
        )  # fmt: skip
    assert result.returncode == 0, result.stderr
    metadata = list(work.glob("*/env.json"))
    assert len(metadata) == 1
    env = json.loads(metadata[0].read_text())
    assert env["kube_cli"] == ("oc" if cli == "oc" else None)
    assert env["kube_cli_requested"] == cli
    assert "kube CLI" not in result.stdout
    if cli == "":
        assert not (tmp_path / "kubectl.calls").exists()
        assert not (tmp_path / "oc.calls").exists()


def selector_profile(tmp_path):
    path = tmp_path / "profile.yaml"
    raw = yaml.safe_load(profile_yaml("http://localhost:1"))
    raw["environment"] = {
        "namespace": "ns",
        "context": "profile-context",
        "kubeconfig": str(tmp_path / "profile config"),
    }
    raw["ingresses"][0]["setup"] = {
        "requires": [{"kind": "port_forward", "selector": "app=test",
                      "local_port": 18100, "remote_port": 8000}]
    }  # fmt: skip
    path.write_text(yaml.safe_dump(raw))
    return loader.load(path)


@pytest.mark.parametrize("cli", [None, "oc", ""])
def test_unavailable_cli_setup_preserves_override_and_context(tmp_path, monkeypatch, cli):
    profile = selector_profile(tmp_path)
    if cli is not None:
        make_cli(tmp_path, "kubectl")
    monkeypatch.setenv("PATH", str(tmp_path))
    config = tmp_path / "override config"
    kube = KubeContext(kubeconfig=config, context="override-context", cli=cli)
    assert not kube.available
    monkeypatch.setattr(KubeContext, "_run", lambda *args, **kwargs: pytest.fail("cluster read"))

    command = preflight._command_for(profile.ingress("fake").setup.requires[0], profile, kube)
    assert shlex.split(command) == [
        "kubectl" if cli is None else cli,
        "--context", "override-context",
        "-n", "ns", "port-forward", "-l", "app=test", "18100:8000",
    ]  # fmt: skip


def test_setup_without_kube_uses_profile_context_and_unresolved_selector(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    profile = selector_profile(tmp_path)
    command = preflight._command_for(profile.ingress("fake").setup.requires[0], profile, None)
    assert shlex.split(command) == [
        "kubectl", "--context", "profile-context", "-n", "ns", "port-forward",
        "-l", "app=test", "18100:8000",
    ]  # fmt: skip


@pytest.mark.parametrize("cli", [None, "oc", ""])
def test_profile_setup_warns_and_prints_unresolved_selector_without_cli(tmp_path, cli):
    profile = selector_profile(tmp_path)
    if cli is not None:
        make_cli(tmp_path, "kubectl")
    config = tmp_path / "override config"
    result = run_llmdv(
        "profile", "show", str(profile.source_path), "--setup",
        "--kubeconfig", str(config), "--context", "override-context",
        *(["--kube-cli", cli] if cli is not None else []),
        env={"PATH": str(tmp_path)},
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    reason = NO_CLI_REASON if cli is None else f"requested cluster CLI {cli!r}"
    assert reason in result.stderr
    assert "selectors will be printed unresolved" in result.stderr
    command = next(line for line in result.stdout.splitlines() if "port-forward" in line)
    assert shlex.split(command) == [
        "kubectl" if cli is None else cli,
        "--context", "override-context",
        "-n", "ns", "port-forward", "-l", "app=test", "18100:8000",
    ]  # fmt: skip
    assert "pod/pod-a" not in command
    assert not (tmp_path / "kubectl.calls").exists()


@pytest.mark.parametrize("cli", [None, "oc"])
def test_setup_uses_short_cli_name_and_context_without_local_paths(tmp_path, monkeypatch, cli):
    make_cli(tmp_path, "kubectl")
    make_cli(tmp_path, "oc")
    monkeypatch.setenv("PATH", str(tmp_path))
    profile = selector_profile(tmp_path)
    config = tmp_path / "config file"
    kube = KubeContext(kubeconfig=config, context="ctx", cli=cli)

    command = preflight._command_for(profile.ingress("fake").setup.requires[0], profile, kube)
    name = cli or "kubectl"
    assert command.startswith(f"{name} --context ctx ")
    assert "--kubeconfig" not in command
    assert str(tmp_path) not in command
    assert shlex.split(command)[0] == name
    assert kube._base() == [str(tmp_path / name), "--kubeconfig", str(config), "--context", "ctx"]
    assert "port-forward pod/pod-a" in command


@pytest.mark.parametrize("cli", [None, "oc", ""])
def test_log_collection_reuses_the_preflight_reason(tmp_path, monkeypatch, cli):
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(preflight, "_reachable", lambda ingress: (True, 1.0))
    # No fleet stands behind the stub, so its model list cannot be read either.
    monkeypatch.setattr(preflight, "_check_access", lambda report, ingress: None)
    profile = selector_profile(tmp_path)
    kube = KubeContext(cli=cli)
    report = preflight.run(profile, kube=kube)
    reason = kube.unavailable_reason
    assert report.log_access is preflight.LogAccess.NO_CLUSTER_CLI
    assert report.log_access_reason == reason
    assert len(report.warnings) == 1
    assert reason in report.warnings[0].message

    def recomputed(self):
        pytest.fail("collection must reuse the preflight reason")

    monkeypatch.setattr(KubeContext, "unavailable_reason", property(recomputed))
    collection = verify_cmd._collect_logs(
        SimpleNamespace(pod_logs="failures"),
        profile,
        ["fake"],
        kube,
        tmp_path,
        None,
        [{"outcome": "fail"}],
        report,
    )
    assert collection.reason == reason
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize("coordinate", [None, "cluster", "namespace", "context", "kubeconfig"])
@pytest.mark.parametrize("available", [False, True])
def test_banner_only_shows_cli_for_a_declared_environment(
    tmp_path, monkeypatch, coordinate, available
):
    monkeypatch.setenv("PATH", str(tmp_path))
    if available:
        make_cli(tmp_path, "oc")
    raw = yaml.safe_load(profile_yaml("http://localhost:1"))
    if coordinate:
        raw["environment"] = {coordinate: "test-value"}
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(raw))
    profile = loader.load(path)
    output = []
    monkeypatch.setattr(banner, "emit", output.append)
    banner.start(profile, str(path), ["fake"], SimpleNamespace(), tmp_path, [], kube=KubeContext())
    lines = [line for line in output if "kube CLI" in line]
    assert len(lines) == (1 if coordinate else 0)
    if lines:
        assert ("oc" if available else "unavailable") in lines[0]


@pytest.mark.parametrize(
    "argv",
    [
        ["--kube-cli", "", "verify", "--profile", "p.yaml"],
        ["--kube-cli", "", "profile", "show", "p.yaml", "--setup"],
        ["profile", "--kube-cli", "", "show", "p.yaml", "--setup"],
    ],
)
def test_empty_override_before_a_subcommand_is_preserved(argv):
    args, unknown = build_parser().parse_known_args(argv)
    assert args.kube_cli == ""
    assert not unknown
