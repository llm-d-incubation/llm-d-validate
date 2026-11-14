"""Preflight decides whether a failure is the fleet's fault or the operator's.

That distinction is the reason exit code 3 exists, so these cases check the classification as
much as the detection.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from llmdv import preflight
from llmdv.kube import KubeContext
from llmdv.profile import loader
from tests.fakes import MODEL, FakeFleet, FakeModelDiscoveryConfig

TEMPLATE = """
schema: llmdv.profile/1
name: pf
model: {{public_id: {model}}}
ingresses:
  - id: one
    kind: vllm
    base_url: {base_url}
    health_path: /health
    surfaces: [chat_completions]
    auth: {auth}
    setup:
      proxy: {proxy}
      requires:
        - kind: port_forward
          namespace: ns
          selector: app=thing
          local_port: 18100
          remote_port: 8000
"""


def build(tmp_path, base_url, auth="{type: none}", proxy="bypass", model=MODEL):
    path = tmp_path / "pf.yaml"
    text = TEMPLATE.format(base_url=base_url, auth=auth, proxy=proxy, model=model)
    path.write_text(textwrap.dedent(text))
    return loader.load(path)


def test_reachable_fleet_passes_preflight(tmp_path):
    with FakeFleet() as fleet:
        report = preflight.run(build(tmp_path, fleet.base_url))
    assert report.ok
    assert report.reachable == {"one"}


def test_unreachable_endpoint_is_a_reachability_problem_with_a_remedy(tmp_path):
    # Port 1 is reserved and nothing listens there.
    report = preflight.run(build(tmp_path, "http://127.0.0.1:1"))
    assert not report.ok
    problem = report.problems[0]
    assert problem.kind == "reachability"
    assert problem.ingress_id == "one"
    # The remedy is the port-forward command, so the operator is not left guessing.
    assert "port-forward" in (problem.remedy or "")


def test_missing_credential_is_a_credentials_problem(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMDV_PF_KEY", raising=False)
    with FakeFleet() as fleet:
        report = preflight.run(
            build(tmp_path, fleet.base_url, auth="{type: bearer, key_env: LLMDV_PF_KEY}")
        )
    assert not report.ok
    assert report.problems[0].kind == "credentials"
    assert "LLMDV_PF_KEY" in report.problems[0].remedy


# ------------------------------------------------- the key works and reaches the model

BEARER = "{type: bearer, key_env: LLMDV_PF_KEY}"


def test_a_rejected_key_is_a_credentials_problem(tmp_path, monkeypatch):
    """The health path is unauthenticated, so only the model list can tell a wrong key."""
    monkeypatch.setenv("LLMDV_PF_KEY", "sk-wrong")
    with FakeFleet(auth_header=("Authorization", "Bearer sk-right")) as fleet:
        report = preflight.run(build(tmp_path, fleet.base_url, auth=BEARER))
    assert not report.ok
    assert [problem.kind for problem in report.problems] == ["credentials"]
    assert "401" in report.problems[0].message
    assert "$LLMDV_PF_KEY" in report.problems[0].message
    # Reachable and refused are different findings: the fleet answered.
    assert report.reachable == {"one"}


def test_an_accepted_key_with_the_model_listed_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMDV_PF_KEY", "sk-right")
    with FakeFleet(auth_header=("Authorization", "Bearer sk-right")) as fleet:
        report = preflight.run(build(tmp_path, fleet.base_url, auth=BEARER))
    assert report.ok, [problem.render() for problem in report.problems]
    assert [warning for warning in report.warnings if warning.kind == "access"] == []


def test_a_model_the_key_cannot_use_is_an_access_problem(tmp_path):
    """LiteLLM lists only what a key may call, so an absent id fails every case after it."""
    with FakeFleet(model="fake/Other-7B") as fleet:
        report = preflight.run(build(tmp_path, fleet.base_url))
    assert not report.ok
    assert [problem.kind for problem in report.problems] == ["access"]
    message = report.problems[0].message
    assert repr(MODEL) in message and "fake/Other-7B" in message


@pytest.mark.parametrize("defect", ["absent", "forbidden"])
def test_a_list_that_cannot_be_read_is_only_a_warning(tmp_path, defect):
    """No list route, or a key scoped away from listing, cannot decide; the cases still can."""
    config = FakeModelDiscoveryConfig(listing_defect=defect)
    with FakeFleet(model="fake/Other-7B", model_discovery_config=config) as fleet:
        report = preflight.run(build(tmp_path, fleet.base_url))
    assert report.ok, [problem.render() for problem in report.problems]
    access = [warning for warning in report.warnings if warning.kind == "access"]
    assert len(access) == 1
    assert ("404" if defect == "absent" else "403") in access[0].message


def test_proxy_bypass_with_an_active_proxy_is_rejected(tmp_path, monkeypatch):
    """An unnoticed proxy hop changes results silently, which is worse than failing."""
    monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    with FakeFleet() as fleet:
        report = preflight.run(build(tmp_path, fleet.base_url))
    assert not report.ok
    assert report.problems[0].kind == "proxy"


def test_no_proxy_covering_the_host_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:8080")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    with FakeFleet() as fleet:
        report = preflight.run(build(tmp_path, fleet.base_url))
    assert report.ok


def test_proxy_allow_skips_the_proxy_check(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    with FakeFleet() as fleet:
        report = preflight.run(build(tmp_path, fleet.base_url, proxy="allow"))
    assert report.ok


def test_unsupported_remote_output_fails_before_any_test_runs(tmp_path):
    with FakeFleet() as fleet:
        report = preflight.run(build(tmp_path, fleet.base_url), remote_output="pvc://results/run/")
    assert not report.ok
    assert report.problems[0].kind == "output"


def test_unsupported_artifact_sink_fails_preflight(tmp_path):
    with FakeFleet() as fleet:
        report = preflight.run(build(tmp_path, fleet.base_url), artifact_sink="pvc://results/run/")
    assert not report.ok
    assert report.problems[0].kind == "artifact_sink"
    assert "--artifact-sink" in report.problems[0].message


def test_read_only_artifact_sink_fails_preflight(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write read-only directories")
    root = tmp_path / "read-only"
    root.mkdir()
    root.chmod(0o500)
    try:
        with FakeFleet() as fleet:
            report = preflight.run(build(tmp_path, fleet.base_url), artifact_sink=f"file://{root}")
        assert not report.ok
        assert report.problems[0].kind == "artifact_sink"
    finally:
        root.chmod(0o700)


def test_setup_commands_are_resolved_even_without_a_kubeconfig(tmp_path):
    """Without cluster access the selector is printed unresolved rather than omitted."""
    with FakeFleet() as fleet:
        report = preflight.run(build(tmp_path, fleet.base_url))
    command = report.resolved_setup["one"][0]
    assert "port-forward" in command and "app=thing" in command


def _stub_kubectl(tmp_path, monkeypatch, allow_pods: bool, allow_logs: bool | None = None) -> None:
    """A kubectl that refuses cluster-scoped reads, as a namespaced Role does.

    `pods` and `pods/log` answer separately because they are separate RBAC verbs: granting
    `list` on pods does not grant reading their logs.
    """
    allow_logs = allow_pods if allow_logs is None else allow_logs
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    pods = "exit 0" if allow_pods else "echo 'Error from server (Forbidden): pods' >&2; exit 1"
    # NotFound is the permitted answer: the probe pod is never meant to exist.
    logs = (
        "echo 'Error from server (NotFound): pods \"llmdv-access-probe\" not found' >&2; exit 1"
        if allow_logs
        else "echo 'Error from server (Forbidden): pods/log' >&2; exit 1"
    )
    log_path = "/api/v1/namespaces/ns/pods/llmdv-access-probe/log?tailLines=1"
    kubectl.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        f'  *" get pods "*) {pods} ;;\n'
        f'  *" get --raw {log_path} "*) {logs} ;;\n'
        "  *) echo 'Error from server (Forbidden): cannot get resource at the cluster scope' >&2;"
        " exit 1 ;;\n"
        "esac\n"
    )
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")


def _report_with_namespace(tmp_path):
    with FakeFleet() as fleet:
        path = tmp_path / "ns.yaml"
        path.write_text(
            textwrap.dedent(TEMPLATE.format(base_url=fleet.base_url, auth="{type: none}",
                                            proxy="bypass", model=MODEL))
            + "environment: {namespace: ns}\n"
        )  # fmt: skip
        return preflight.run(loader.load(path), kube=KubeContext(namespace="ns"))


def test_a_namespaced_role_passes_preflight_without_a_warning(tmp_path, monkeypatch):
    """The Role a scheduled job runs with can list pods but never read the Namespace object.

    Preflight used to read the Namespace, which is cluster-scoped, so every in-cluster run under
    that Role exited 3 before testing anything.
    """
    _stub_kubectl(tmp_path, monkeypatch, allow_pods=True)
    report = _report_with_namespace(tmp_path)
    assert report.ok, [problem.render() for problem in report.problems]
    assert report.warnings == []


def test_no_cluster_access_warns_but_does_not_stop_the_run(tmp_path, monkeypatch):
    """A wrong context still gets said, but cluster access only serves selectors and logs."""
    _stub_kubectl(tmp_path, monkeypatch, allow_pods=False)
    report = _report_with_namespace(tmp_path)
    assert report.ok, [problem.render() for problem in report.problems]
    assert report.reachable == {"one"}
    # Two separate verbs, so two separate findings: a selector cannot be resolved, and the
    # pod logs that the port-forward requirement implies cannot be read either.
    assert [warning.kind for warning in report.warnings] == ["environment", "environment"]
    assert "not listable" in report.warnings[0].message
    assert "forbidden" in report.warnings[1].message


def build_from_env(tmp_path, url_line):
    path = tmp_path / "pf.yaml"
    text = textwrap.dedent(
        TEMPLATE.format(base_url="X", auth="{type: none}", proxy="bypass", model=MODEL)
    )
    path.write_text(text.replace("base_url: X", url_line))
    return loader.load(path)


def test_base_url_env_supplies_the_url(tmp_path, monkeypatch):
    """A committed profile names the variable, so the hostname never enters the repository."""
    with FakeFleet() as fleet:
        monkeypatch.setenv("LLMDV_PF_URL", fleet.base_url + "/")
        profile = build_from_env(tmp_path, "base_url_env: LLMDV_PF_URL")
        assert profile.ingresses[0].base_url == fleet.base_url
        report = preflight.run(profile)
    assert report.ok
    assert report.reachable == {"one"}


def test_unset_base_url_env_is_a_base_url_problem_not_a_profile_error(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMDV_PF_URL", raising=False)
    profile = build_from_env(tmp_path, "base_url_env: LLMDV_PF_URL")
    # Offline validation needs no environment, so it must not fail on the unset variable.
    assert not [d for d in loader.validate(profile) if d.level == "error" and "base_url" in d.path]
    report = preflight.run(profile)
    assert not report.ok
    assert report.problems[0].kind == "base_url"
    assert "LLMDV_PF_URL" in report.problems[0].remedy


def test_base_url_and_base_url_env_together_are_rejected(tmp_path):
    with pytest.raises(loader.ProfileError, match="not both"):
        build_from_env(tmp_path, "base_url: http://h\n    base_url_env: LLMDV_PF_URL")


def build_redirect_from_env(tmp_path):
    path = tmp_path / "pf.yaml"
    text = textwrap.dedent(
        TEMPLATE.format(base_url="X", auth="{type: none}", proxy="bypass", model=MODEL)
    )
    text = text.replace("base_url: X", "base_url_env: LLMDV_PF_URL")
    path.write_text(text + "    transport:\n      http_redirect: expected\n")
    return loader.load(path)


@pytest.mark.parametrize("value", ["http://127.0.0.1:1", "https://127.0.0.1:8443"])
def test_http_redirect_on_a_base_url_env_it_cannot_probe_is_a_base_url_problem(
    tmp_path, monkeypatch, value
):
    monkeypatch.setenv("LLMDV_PF_URL", value)
    report = preflight.run(build_redirect_from_env(tmp_path))
    problems = [p for p in report.problems if p.kind == "base_url"]
    (problem,) = problems
    assert problem.ingress_id == "one"
    assert "LLMDV_PF_URL" in problem.message and "LLMDV_PF_URL" in problem.remedy
    assert "transport.http_redirect" in problem.message
    # The value is a hostname, so the problem names the variable and never quotes it.
    assert "127.0.0.1" not in problem.render()


def test_http_redirect_on_an_https_base_url_env_is_not_a_base_url_problem(tmp_path, monkeypatch):
    # Nothing answers there, so reachability fails; only the base_url check is under test.
    monkeypatch.setenv("LLMDV_PF_URL", "https://127.0.0.1")
    report = preflight.run(build_redirect_from_env(tmp_path))
    assert not [p for p in report.problems if p.kind == "base_url"]
