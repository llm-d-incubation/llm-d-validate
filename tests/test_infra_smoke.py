"""Infrastructure smoke launcher and uploads, with no cluster or real credentials."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.fakes import FakeIAM, FakeS3, ibm_cos_env, s3_env

FUNCTIONAL = Path(__file__).resolve().parents[1]
DEPLOY = FUNCTIONAL / "deploy"
TOKEN = "llmdv-ibm-access-token-1"


def _probe(tmp_path, env=None, *, uid=1001):
    # Emulate the pod's UID so these tests also run under root in CI. The SDK, HTTP,
    # storage writes and publisher are real; all credentials and endpoints are synthetic.
    return subprocess.run(
        [sys.executable, "-c", (
            "import runpy, sys\n"
            "from unittest.mock import patch\n"
            "with patch('os.getuid', return_value=int(sys.argv[2])):\n"
            "    runpy.run_path(sys.argv[1], run_name='__main__')\n"
        ), str(DEPLOY / "infra-smoke.py"), str(uid)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(FUNCTIONAL / "src"),
            "LLMDV_WORK_DIR": str(tmp_path / "runs"),
            "LLMDV_RESULTS_URI": "s3://results/smoke",
            "LLMDV_PROFILE": str(tmp_path / "does-not-exist.yaml"),
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": "/nonexistent",
            "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent",
            **(env or {}),
        },
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip


@pytest.fixture(params=["aws", "ibm-iam"])
def backend(request):
    mode = request.param
    pytest.importorskip("ibm_boto3" if mode == "ibm-iam" else "boto3")
    rejected_secret = f"{FakeIAM.api_key} {TOKEN}" if mode == "ibm-iam" else "llmdv-test-secret"
    with (
        FakeIAM() as iam,
        FakeS3(
            bearer_tokens=(TOKEN,) if mode == "ibm-iam" else (),
            deny_message=f"denied: {rejected_secret}",
        ) as store,
    ):
        env = ibm_cos_env(store, iam) if mode == "ibm-iam" else s3_env(store)
        yield store, iam, env


def test_smoke_publishes_without_a_profile_and_never_claims_inference_passed(tmp_path, backend):
    store, iam, env = backend
    # Leaving auth unset must continue to select the default AWS provider.
    if env["LLMDV_S3_AUTH"] == "aws":
        env.pop("LLMDV_S3_AUTH")
    result = _probe(tmp_path, env)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["functional_tests"] == "not_run"
    assert report["publication"]["status"] == "published"
    prefix = f"results/smoke/infra-smoke/{report['run_id']}/"
    assert store.order == [prefix + "infrastructure.json", prefix + "MANIFEST.json"]
    data = store.objects[store.order[0]]
    uploaded = json.loads(data)
    assert uploaded["functional_tests"] == "not_run"
    assert set(uploaded["checks"].values()) == {"passed"}
    assert "verification" not in uploaded
    manifest = json.loads(store.objects[store.order[-1]])
    assert manifest["files"] == [{
        "path": "infrastructure.json", "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }]  # fmt: skip
    assert data == (tmp_path / "runs" / report["run_id"] / "infrastructure.json").read_bytes()
    if env.get("LLMDV_S3_AUTH") == "ibm-iam":
        assert iam.received
        assert all(headers["Authorization"] == f"Bearer {TOKEN}" for headers in store.headers)
    else:
        assert not iam.received
        assert all(
            headers["Authorization"].startswith("AWS4-HMAC-SHA256") for headers in store.headers
        )


def test_smoke_requires_credentials_instead_of_skipping_publication(tmp_path, backend):
    store, iam, env = backend
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "IBM_COS_API_KEY"):
        env.pop(name, None)
    result = _probe(tmp_path, env)

    assert result.returncode == 5, result.stderr
    assert json.loads(result.stdout)["publication"]["status"] == "failed"
    assert not store.headers
    assert not iam.received


@pytest.mark.parametrize("denied", ["infrastructure.json", "MANIFEST.json"])
def test_smoke_fails_if_either_upload_is_rejected_and_scrubs_provider_errors(
    tmp_path, backend, denied
):
    store, iam, env = backend
    store.deny_keys = (denied,)
    result = _probe(tmp_path, env)

    assert result.returncode == 5, result.stderr
    report = json.loads(result.stdout)
    assert report["publication"]["status"] == "failed"
    assert report["functional_tests"] == "not_run"
    assert not any(key.endswith("MANIFEST.json") for key in store.objects)
    output = result.stdout + result.stderr
    assert all(value not in output for value in (iam.api_key, TOKEN, "llmdv-test-secret"))


def test_iam_rejection_is_redacted_in_the_smoke_report(tmp_path):
    pytest.importorskip("ibm_boto3")
    with FakeIAM(reject=True) as iam, FakeS3() as store:
        result = _probe(tmp_path, ibm_cos_env(store, iam))
    assert result.returncode == 5, result.stderr
    assert json.loads(result.stdout)["publication"]["status"] == "failed"
    assert iam.api_key not in result.stdout + result.stderr
    assert not store.headers


@pytest.mark.parametrize("destination", ["", "file:///tmp/results", "https://example.com/results"])
def test_smoke_rejects_a_missing_or_non_s3_destination(tmp_path, destination):
    result = _probe(tmp_path, {"LLMDV_RESULTS_URI": destination})
    assert result.returncode == 2
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("problem", ["root", "storage"])
def test_smoke_rejects_local_execution_problems_before_uploading(tmp_path, problem):
    if problem == "storage":
        (tmp_path / "runs").write_text("a file cannot hold a run directory")
    result = _probe(tmp_path, uid=0 if problem == "root" else 1001)
    assert result.returncode == 3
    assert "Infrastructure smoke environment" in result.stderr
    assert not result.stdout


@pytest.fixture
def launcher(tmp_path):
    if shutil.which("jq") is None:
        pytest.skip("needs jq for the local Job transformation")
    base = yaml.safe_load((DEPLOY / "base" / "cronjob.yaml").read_text())
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "placeholder", "namespace": "example"},
        "spec": base["spec"]["jobTemplate"]["spec"],
    }  # fmt: skip
    # A sidecar and an explicit entrypoint exercise selecting the llmdv container by name.
    job["spec"]["template"]["spec"]["containers"].insert(0, {"name": "sidecar", "image": "example"})
    job["spec"]["template"]["spec"]["containers"][1]["command"] = ["llmdv", "job"]
    rendered = tmp_path / "rendered.json"
    rendered.write_text(json.dumps(job))
    created = tmp_path / "created.json"
    calls = tmp_path / "calls.jsonl"
    fake_cli = tmp_path / "kubectl"
    fake_cli.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["SMOKE_CALLS"]).open("a") as log:
    log.write(json.dumps(args) + "\\n")
if "--dry-run=client" in args:
    sys.stdout.write(Path(os.environ["SMOKE_RENDERED"]).read_text())
    sys.exit(1 if os.environ.get("SMOKE_FAIL") == "render" else 0)
if args[-3:] == ["create", "-f", "-"]:
    if os.environ.get("SMOKE_FAIL") == "create":
        sys.exit(1)
    Path(os.environ["SMOKE_CREATED"]).write_text(sys.stdin.read())
    sys.exit(0)
sys.exit(99)
"""
    )
    fake_cli.chmod(0o755)
    (tmp_path / "oc").symlink_to(fake_cli)

    def run(*args, fail=""):
        return subprocess.run(
            ["bash", str(DEPLOY / "infra-smoke.sh"), *args],
            capture_output=True, text=True,
            env={
                "PATH": f"{tmp_path}:" + os.environ.get("PATH", "/usr/bin:/bin"),
                "SMOKE_RENDERED": str(rendered), "SMOKE_CREATED": str(created),
                "SMOKE_CALLS": str(calls), "SMOKE_FAIL": fail,
            },
        )  # fmt: skip

    return run, job, rendered, created, calls


@pytest.mark.parametrize("cli_args", [[], ["--cli", "oc"]])
def test_launcher_preserves_pod_settings_and_selects_namespace_and_cronjob(launcher, cli_args):
    run, template, _, created, calls = launcher
    result = run("-n", "example", "--cronjob", "nightly", *cli_args)
    assert result.returncode == 0, result.stderr
    name = result.stdout.strip()
    assert name.startswith("llmdv-infra-") and len(name) <= 63
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [
        [
            "-n",
            "example",
            "create",
            "job",
            name,
            "--from=cronjob/nightly",
            "--dry-run=client",
            "-o",
            "json",
        ],
        ["-n", "example", "create", "-f", "-"],
    ]
    expected = copy.deepcopy(template)
    expected["metadata"]["labels"] = {"llmdv-run-kind": "infrastructure-smoke"}
    expected["spec"]["activeDeadlineSeconds"] = 300
    expected["spec"]["template"]["metadata"] = {
        "labels": {"llmdv-run-kind": "infrastructure-smoke"}
    }
    container = expected["spec"]["template"]["spec"]["containers"][1]
    container["command"] = ["python", "-c"]
    container["args"] = [(DEPLOY / "infra-smoke.py").read_text()]
    assert json.loads(created.read_text()) == expected


@pytest.mark.parametrize("failure", ["render", "missing_container", "create"])
def test_launcher_never_claims_a_job_was_started_when_rendering_or_creation_fails(
    launcher, failure
):
    run, template, rendered, created, calls = launcher
    if failure == "missing_container":
        template["spec"]["template"]["spec"]["containers"].pop()
        rendered.write_text(json.dumps(template))
    result = run("-n", "example", fail=failure)
    assert result.returncode != 0
    assert not result.stdout
    assert not created.exists()
    assert len(calls.read_text().splitlines()) == (2 if failure == "create" else 1)


@pytest.mark.parametrize("args", [[], ["-n"], ["-n", "--cli", "oc"], ["--cronjob"], ["--unknown"]])
def test_launcher_rejects_invalid_arguments_without_contacting_a_cluster(args):
    result = subprocess.run(
        ["bash", str(DEPLOY / "infra-smoke.sh"), *args], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "infra-smoke.sh:" in result.stderr
