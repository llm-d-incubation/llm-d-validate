"""trigger.sh, through the real `kubectl patch --local` (no cluster) with the rest stubbed."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


CRONJOB_JOB = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {"name": "placeholder", "namespace": "ns"},
    "spec": {
        "backoffLimit": 0,
        "template": {
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "llmdv",
                        "image": "registry.example.com/org/llmdv:sha",
                        "args": ["--label", "nightly-only=yes"],
                        "env": [{"name": "LLMDV_RUN_BUDGET", "value": "3000"}],
                    }
                ],
            }
        },
    },
}


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl for patch --local")
def test_trigger_creates_the_cronjob_template_with_only_its_arguments_replaced(tmp_path):
    real_kubectl = shutil.which("kubectl")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rendered = tmp_path / "rendered.json"
    rendered.write_text(json.dumps(CRONJOB_JOB))
    created = tmp_path / "created.json"
    log = tmp_path / "kubectl.log"
    _script(
        bin_dir / "kubectl",
        f'echo "$*" >> {log}\n'
        'case "$*" in\n'
        f'  *"--dry-run=client"*) cat {rendered} ;;\n'
        f'  *"patch --local"*) exec {real_kubectl} "$@" ;;\n'
        f'  *"create -f -"*) cat > {created} ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    result = subprocess.run(  # noqa: S603 - fixed script
        ["sh", str(DEPLOY / "trigger.sh"), "-n", "ns", "--id", "Release_1.2",
         "--", "--client", "forge", "-k", 'name with "quotes"'],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )  # fmt: skip

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "llmdv-rollout-release-1-2"
    assert "create job llmdv-rollout-release-1-2 --from=cronjob/llmdv" in log.read_text()

    job = json.loads(created.read_text())
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["args"] == [
        "--label", "trigger=rollout", "--label", "rollout=Release_1.2",
        "--client", "forge", "-k", 'name with "quotes"',
    ]  # fmt: skip
    # Everything else is the CronJob's template, untouched.
    assert container["image"] == "registry.example.com/org/llmdv:sha"
    assert container["env"] == [{"name": "LLMDV_RUN_BUDGET", "value": "3000"}]
    assert job["spec"]["backoffLimit"] == 0


def test_trigger_requires_a_namespace():
    result = subprocess.run(  # noqa: S603 - fixed script
        ["sh", str(DEPLOY / "trigger.sh")], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "NAMESPACE is required" in result.stderr


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl for patch --local")
def test_a_long_job_name_is_bounded_and_stays_unique(tmp_path):
    """A long CronJob name and a commit SHA would otherwise exceed Kubernetes' 63 characters."""
    real_kubectl = shutil.which("kubectl")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rendered = tmp_path / "rendered.json"
    rendered.write_text(json.dumps(CRONJOB_JOB))
    _script(
        bin_dir / "kubectl",
        'case "$*" in\n'
        f'  *"--dry-run=client"*) cat {rendered} ;;\n'
        f'  *"patch --local"*) exec {real_kubectl} "$@" ;;\n'
        '  *"create -f -"*) cat > /dev/null ;;\n'
        "esac\n",
    )

    def job_name(commit: str) -> str:
        result = subprocess.run(  # noqa: S603 - fixed script
            ["sh", str(DEPLOY / "trigger.sh"), "-n", "ns",
             "--cronjob", "llm-d-validate-nightly-tests", "--id", commit],
            capture_output=True, text=True, env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        )  # fmt: skip
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    first = job_name("0123456789abcdef0123456789abcdef01234567")
    second = job_name("0123456789abcdef0123456789abcdef0123456f")
    for name in (first, second):
        assert len(name) <= 63, name
        assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name), name
    assert first != second


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl for patch --local")
def test_a_local_profile_uses_an_owned_configmap_and_the_profile_volume_by_name(tmp_path):
    real_kubectl = shutil.which("kubectl")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rendered = tmp_path / "rendered.json"
    template = json.loads(json.dumps(CRONJOB_JOB))
    template["spec"]["template"]["spec"]["volumes"] = [
        {"name": "tmp", "emptyDir": {}},
        {
            "name": "profile",
            "configMap": {
                "name": "llmdv-profile",
                "items": [{"key": "production.yaml", "path": "profile.yaml"}],
                "optional": True,
            },
        },
    ]
    rendered.write_text(json.dumps(template))
    profile = tmp_path / "profile.yaml"
    profile.write_text("vantage: in-cluster # cluster form\nname: p\n")
    job_input = tmp_path / "job.json"
    configmap = tmp_path / "configmap.json"
    _script(
        bin_dir / "oc",
        f'case "$*" in\n'
        f'  *"create job "*"--dry-run=client"*) cat {rendered} ;;\n'
        f'  *"patch --local"*) exec {real_kubectl} "$@" ;;\n'
        f"  *jsonpath*) cat > {job_input}; printf uid-123 ;;\n"
        f'  *"create configmap "*"--dry-run=client"*) exec {real_kubectl} "$@" ;;\n'
        f'  *"create -f -"*) cat > {configmap} ;;\n'
        f"  *) exit 1 ;;\n"
        f"esac\n",
    )
    _script(bin_dir / "kubectl", "echo kubectl-called >&2; exit 99\n")
    result = subprocess.run(
        [
            "sh",
            str(DEPLOY / "trigger.sh"),
            "-n",
            "ns",
            "--cli",
            "oc",
            "--trigger",
            "manual",
            "--id",
            "operator",
            "--profile-file",
            str(profile),
        ],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "llmdv-manual-operator"
    assert "kubectl-called" not in result.stderr
    job = json.loads(job_input.read_text())
    assert job["spec"]["template"]["spec"]["containers"][0]["args"][:4] == [
        "--label",
        "trigger=manual",
        "--label",
        "manual=operator",
    ]
    volumes = {volume["name"]: volume for volume in job["spec"]["template"]["spec"]["volumes"]}
    assert volumes["profile"] == {"name": "profile", "configMap": {"name": "llmdv-manual-operator"}}
    assert volumes["tmp"] == {"name": "tmp", "emptyDir": {}}
    cm = json.loads(configmap.read_text())
    assert cm["data"] == {"profile.yaml": profile.read_text()}
    assert cm["immutable"] is True
    assert cm["metadata"]["labels"] == {"app.kubernetes.io/name": "llmdv"}
    assert cm["metadata"]["ownerReferences"] == [
        {"apiVersion": "batch/v1", "kind": "Job", "name": "llmdv-manual-operator", "uid": "uid-123"}
    ]


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl for patch --local")
def test_a_manual_trigger_keeps_the_image_and_environment(tmp_path):
    real_kubectl = shutil.which("kubectl")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rendered = tmp_path / "rendered.json"
    rendered.write_text(json.dumps(CRONJOB_JOB))
    created = tmp_path / "created.json"
    _script(
        bin_dir / "kubectl",
        f'case "$*" in\n'
        f'  *"--dry-run=client"*) cat {rendered} ;;\n'
        f'  *"patch --local"*) exec {real_kubectl} "$@" ;;\n'
        f'  *"create -f -"*) cat > {created} ;;\n'
        f"  *) exit 1 ;;\n"
        f"esac\n",
    )
    result = subprocess.run(
        ["sh", str(DEPLOY / "trigger.sh"), "-n", "ns", "--trigger", "manual", "--id", "m"],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    job = json.loads(created.read_text())
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["args"][:4] == ["--label", "trigger=manual", "--label", "manual=m"]
    original = CRONJOB_JOB["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == original["image"]
    assert container["env"] == original["env"]


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl for patch --local")
def test_a_failed_configmap_create_reports_the_job_and_delete_command(tmp_path):
    real_kubectl = shutil.which("kubectl")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rendered = tmp_path / "rendered.json"
    rendered.write_text(json.dumps(CRONJOB_JOB))
    profile = tmp_path / "profile.yaml"
    profile.write_text("vantage: in-cluster\n")
    _script(
        bin_dir / "kubectl",
        f'case "$*" in\n'
        f'  *"create job "*"--dry-run=client"*) cat {rendered} ;;\n'
        f'  *"patch --local"*) exec {real_kubectl} "$@" ;;\n'
        f"  *jsonpath*) printf uid-123 ;;\n"
        f'  *"create configmap "*"--dry-run=client"*) exec {real_kubectl} "$@" ;;\n'
        f'  *"create -f -"*) exit 1 ;;\n'
        f"esac\n",
    )
    result = subprocess.run(
        ["sh", str(DEPLOY / "trigger.sh"), "-n", "ns", "--profile-file", str(profile), "--id", "x"],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert result.returncode != 0
    assert "llmdv-rollout-x" in result.stderr
    assert "delete job llmdv-rollout-x" in result.stderr


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl for patch --local")
def test_a_lost_job_create_response_reports_cleanup_and_skips_configmap_create(tmp_path):
    real_kubectl = shutil.which("kubectl")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rendered = tmp_path / "rendered.json"
    rendered.write_text(json.dumps(CRONJOB_JOB))
    actual_configmap_create = tmp_path / "actual-configmap-create"
    profile = tmp_path / "profile.yaml"
    profile.write_text("vantage: in-cluster\n")
    _script(
        bin_dir / "kubectl",
        f'case "$*" in\n'
        f'  *"create job "*"--dry-run=client"*) cat {rendered} ;;\n'
        f'  *"patch --local"*) exec {real_kubectl} "$@" ;;\n'
        f"  *jsonpath*) exit 1 ;;\n"
        f'  *"create configmap "*"--dry-run=client"*) exec {real_kubectl} "$@" ;;\n'
        f'  *"create -f -"*) touch {actual_configmap_create}; exit 1 ;;\n'
        f"esac\n",
    )
    result = subprocess.run(
        ["sh", str(DEPLOY / "trigger.sh"), "-n", "ns", "--profile-file", str(profile), "--id", "x"],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert result.returncode != 0
    assert "llmdv-rollout-x" in result.stderr
    assert "delete job llmdv-rollout-x" in result.stderr
    assert not actual_configmap_create.exists()


@pytest.mark.parametrize("contents", ["vantage: external\n", "name: p\n"])
def test_a_local_profile_must_be_in_cluster_before_any_cluster_call(tmp_path, contents):
    profile = tmp_path / "profile.yaml"
    profile.write_text(contents)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    _script(bin_dir / "kubectl", f"echo called >> {calls}\n")
    result = subprocess.run(
        ["sh", str(DEPLOY / "trigger.sh"), "-n", "ns", "--profile-file", str(profile)],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert result.returncode == 2
    assert "in-cluster" in result.stderr
    assert not calls.exists()


def test_a_missing_local_profile_is_rejected_before_any_cluster_call(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    _script(bin_dir / "kubectl", f"echo called >> {calls}\n")
    result = subprocess.run(
        ["sh", str(DEPLOY / "trigger.sh"), "-n", "ns", "--profile-file", str(tmp_path / "missing")],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert result.returncode == 2
    assert "missing or unreadable" in result.stderr
    assert not calls.exists()
