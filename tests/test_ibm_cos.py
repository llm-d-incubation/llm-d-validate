"""IBM IAM publishing against local token and object-store servers, without AWS credentials."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from urllib.parse import quote_plus

import pytest

from llmdv.http import redact
from llmdv.output import sink_for
from llmdv.output.remote import UnsupportedRemote
from llmdv.output.s3 import missing_credentials_reason
from tests.fakes import FakeFleet, FakeIAM, FakeS3, ibm_cos_env, profile_yaml, run_llmdv

pytest.importorskip("ibm_boto3", reason="the s3 extra is not installed")

URI = "s3://results/nightly"
TOKENS = ("llmdv-ibm-access-token-1", "llmdv-ibm-access-token-2")


@pytest.fixture(autouse=True)
def isolate_auth(monkeypatch):
    monkeypatch.setenv("LLMDV_S3_AUTH", "ibm-iam")
    for name in (
        "IBM_COS_API_KEY",
        "IBM_COS_SERVICE_INSTANCE_ID",
        "IBM_COS_ENDPOINT",
        "IBM_COS_AUTH_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    # Successful unit tests start the SDK's background refresher. Stop it before the fake
    # servers disappear, so it cannot make requests after the test has finished.
    from ibm_botocore.credentials import DefaultTokenManager

    managers = []
    original = DefaultTokenManager.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        managers.append(self)

    monkeypatch.setattr(DefaultTokenManager, "__init__", init)
    yield managers
    for manager in managers:
        manager._cleanup()


def _run(tmp_path, fleet, env, *, job=False):
    profile = tmp_path / "fake.yaml"
    profile.write_text(profile_yaml(fleet.base_url, fleet.model))
    run_dir = tmp_path / "run"
    result = run_llmdv(
        "job" if job else "verify", "--profile", str(profile),
        "--work-dir" if job else "--local-output", str(run_dir),
        "--remote-output", URI, "--pod-logs", "off", "--log-level", "DEBUG",
        "-k", "models_lists", env=env,
    )  # fmt: skip
    if job and run_dir.exists():
        run_dir = next(run_dir.iterdir())
    path = run_dir / "outcome.json"
    outcome = json.loads(path.read_text()) if path.exists() else None
    return result, run_dir, outcome


def _assert_no_secrets(result, run_dir, store, secrets):
    outputs = [result.stdout.encode(), result.stderr.encode(), *store.objects.values()]
    outputs.extend(path.read_bytes() for path in run_dir.rglob("*") if path.is_file())
    for secret in secrets:
        assert all(secret.encode() not in output for output in outputs), secret


@pytest.mark.parametrize("job", [False, True], ids=["verify", "job"])
def test_iam_publishes_with_bearer_auth_and_manifest_last(tmp_path, job):
    with FakeFleet() as fleet, FakeIAM() as iam, FakeS3(bearer_tokens=TOKENS) as store:
        result, run_dir, outcome = _run(tmp_path, fleet, ibm_cos_env(store, iam), job=job)

    assert result.returncode == 0, result.stderr
    assert outcome["verification"]["counts"] == {"pass": 1}
    assert outcome["publication"]["status"] == "published"
    # Preflight and publication own separate clients. Publication reuses one token for all files.
    assert len(iam.received) == 2
    assert iam.received[0]["apikey"] == [iam.api_key]
    assert store.order[0] == "results/nightly/.llmdv-write-check"
    assert store.order[-1].endswith("/MANIFEST.json")
    prefix = store.order[-1].removesuffix("MANIFEST.json")
    manifest = json.loads(store.objects[prefix + "MANIFEST.json"])
    for entry in manifest["files"]:
        assert hashlib.sha256(store.objects[prefix + entry["path"]]).hexdigest() == entry["sha256"]
    assert store.headers[0]["Authorization"] == f"Bearer {TOKENS[0]}"
    assert all(headers["Authorization"] == f"Bearer {TOKENS[1]}" for headers in store.headers[1:])
    assert all(iam.api_key not in json.dumps(headers) for headers in store.headers)
    for headers in store.headers:
        names = {name.lower() for name in headers}
        assert not any(name.startswith("x-amz-checksum-") for name in names)
        assert not names & {"x-amz-sdk-checksum-algorithm", "x-amz-trailer"}
        assert "aws-chunked" not in headers.get("Content-Encoding", "").lower()
    _assert_no_secrets(result, run_dir, store, [iam.api_key, iam.refresh_token, *iam.tokens])


def test_standalone_publish_uses_iam(tmp_path):
    (tmp_path / "results.jsonl").write_text("")
    with FakeIAM() as iam, FakeS3(bearer_tokens=TOKENS) as store:
        result = run_llmdv(
            "publish", str(tmp_path), "--exit-code", "0", "--remote-output", URI,
            env=ibm_cos_env(store, iam),
        )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert store.order[-1].endswith("/MANIFEST.json")
    assert iam.received


@pytest.mark.parametrize("missing", ["IBM_COS_API_KEY", "IBM_COS_SERVICE_INSTANCE_ID"])
@pytest.mark.parametrize("job", [False, True], ids=["verify", "job"])
def test_missing_iam_credentials_never_fall_back_to_aws(tmp_path, missing, job):
    with FakeFleet() as fleet, FakeIAM() as iam, FakeS3(bearer_tokens=TOKENS) as store:
        env = ibm_cos_env(store, iam) | {
            missing: "   ",
            "AWS_ACCESS_KEY_ID": "ambient-key",
            "AWS_SECRET_ACCESS_KEY": "ambient-secret",
        }
        result, _, outcome = _run(tmp_path, fleet, env, job=job)
    assert result.returncode == (0 if job else 3), result.stderr
    assert outcome["publication"]["status"] == ("skipped" if job else "failed")
    assert missing in json.dumps(outcome)
    assert iam.received == []
    assert store.headers == []
    if job:
        assert outcome["verification"]["counts"] == {"pass": 1}


def test_missing_iam_credentials_can_publish_to_the_pvc_fallback(tmp_path):
    profile = tmp_path / "fake.yaml"
    with FakeFleet() as fleet:
        profile.write_text(profile_yaml(fleet.base_url, fleet.model))
        result = run_llmdv(
            "job", "--profile", str(profile), "--work-dir", str(tmp_path / "work"),
            "--remote-output", URI, "--fallback-output", f"file://{tmp_path / 'pvc'}",
            "--pod-logs", "off", "-k", "models_lists", env={"LLMDV_S3_AUTH": "ibm-iam"},
        )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert list((tmp_path / "pvc").rglob("MANIFEST.json"))


def test_iam_rejection_is_a_failure_and_scrubs_credentials_everywhere(tmp_path):
    with FakeFleet() as fleet, FakeIAM(reject=True) as iam, FakeS3() as store:
        iam.api_key = "llmdv-key-with+encoded/characters"
        result, run_dir, outcome = _run(tmp_path, fleet, ibm_cos_env(store, iam), job=True)
    assert result.returncode == 3, result.stderr
    assert outcome["publication"]["status"] == "failed"
    assert "HttpCode(400)" in json.dumps(outcome)
    assert iam.received
    assert store.order == []
    _assert_no_secrets(result, run_dir, store, [iam.api_key, quote_plus(iam.api_key)])


@pytest.mark.parametrize("deny_after, code", [(0, 3), (3, 5)])
def test_cos_rejection_remains_a_delivery_failure(tmp_path, deny_after, code):
    with (
        FakeFleet() as fleet,
        FakeIAM() as iam,
        FakeS3(
            deny_after=deny_after,
            bearer_tokens=TOKENS,
            deny_message=f"denied: {FakeIAM.api_key} {TOKENS[0]}",
        ) as store,
    ):
        result, run_dir, outcome = _run(tmp_path, fleet, ibm_cos_env(store, iam))
    assert result.returncode == code, result.stderr
    assert outcome["publication"]["status"] == "failed"
    assert not any(key.endswith("MANIFEST.json") for key in store.order)
    _assert_no_secrets(result, run_dir, store, [iam.api_key, *iam.tokens])


def test_credential_probe_is_local_and_does_not_use_aws(monkeypatch):
    monkeypatch.setenv("IBM_COS_API_KEY", FakeIAM.api_key)
    monkeypatch.setenv("IBM_COS_SERVICE_INSTANCE_ID", "llmdv-test-instance")
    # No endpoint is needed for a presence check; missing endpoint is a configuration error.
    assert missing_credentials_reason() is None
    with pytest.raises(UnsupportedRemote, match="IBM_COS_ENDPOINT"):
        sink_for(URI).check()


def test_unknown_auth_mode_is_a_usage_error(tmp_path):
    result = run_llmdv(
        "job", "--profile", "not-read.yaml", "--work-dir", str(tmp_path),
        "--remote-output", URI, env={"LLMDV_S3_AUTH": "typo"},
    )  # fmt: skip
    assert result.returncode == 2, result.stderr
    assert "LLMDV_S3_AUTH must be aws or ibm-iam" in result.stderr
    assert not list(tmp_path.iterdir())


def test_expired_token_is_refreshed_and_registered_before_upload(monkeypatch, isolate_auth):
    # Move only the token manager's clock: no sleeps or changes to the real expiry contract.
    with FakeIAM() as iam, FakeS3(bearer_tokens=TOKENS) as store:
        for name, value in ibm_cos_env(store, iam).items():
            monkeypatch.setenv(name, value)
        sink = sink_for(URI)
        sink.check()
        manager = isolate_auth[0]
        future = manager._expiry_time + timedelta(seconds=1)
        monkeypatch.setattr(manager, "_time_fetcher", lambda: future)
        sink.check()
        manager._cleanup()
    assert len(iam.received) == 2
    assert iam.received[1]["refresh_token"] == [iam.refresh_token]
    assert [headers["Authorization"] for headers in store.headers] == [
        f"Bearer {t}" for t in TOKENS
    ]
    assert {*TOKENS, iam.refresh_token} <= redact.known_secrets()
