"""Publishing a run to S3, against a local fake store."""

from __future__ import annotations

import hashlib
import json

import pytest

from llmdv.http import redact
from llmdv.output import sink_for
from llmdv.output.s3 import _register_credentials, missing_credentials_reason
from tests.fakes import FakeFleet, FakeS3, profile_yaml, run_llmdv, s3_env

pytest.importorskip("boto3", reason="the s3 extra is not installed")


# ------------------------------------------------------- credentials reach the redactor
#
# `deliver()` and preflight both wrap S3 errors in `scrub()`, but a scrub can only redact
# values the registry knows. The IBM IAM path registers its credentials; the AWS path did
# not, so the scrub was a no-op there and an S3 error echoing the access key id -- which
# InvalidAccessKeyId and SignatureDoesNotMatch both do -- landed in outcome.json in clear.


@pytest.fixture
def clean_registry():
    """The secret registry is process-global, so put it back as it was."""
    previous = redact.known_secrets()
    redact.reset_secrets()
    yield
    redact.reset_secrets()
    redact.install_secrets(list(previous))


def credentials(access_key="AKIAFAKEKEYID000000", secret_key="fake/secret/value/000000"):
    from botocore.credentials import Credentials

    return Credentials(access_key, secret_key, token="fake-session-token-000")


def test_resolved_credentials_are_registered_for_redaction(clean_registry):
    _register_credentials(credentials())
    known = redact.known_secrets()
    assert "AKIAFAKEKEYID000000" in known
    assert "fake/secret/value/000000" in known
    assert "fake-session-token-000" in known


def test_a_registered_credential_is_scrubbed_out_of_an_error_message(clean_registry):
    """The end the bug was at: an S3 error echoing the key id, on its way to outcome.json."""
    _register_credentials(credentials())
    message = redact.scrub(
        "An error occurred (InvalidAccessKeyId): The AWS Access Key Id you provided "
        "does not exist in our records. AKIAFAKEKEYID000000"
    )
    assert "AKIAFAKEKEYID000000" not in message
    assert redact.SENTINEL in message
    # The rest of the message survives, or the operator cannot tell what went wrong.
    assert "InvalidAccessKeyId" in message


def test_no_credentials_registers_nothing_rather_than_raising(clean_registry):
    """An unauthenticated environment is a reason to report, not a traceback."""
    _register_credentials(None)
    assert redact.known_secrets() == frozenset()


def test_probing_for_credentials_registers_them(monkeypatch, clean_registry):
    """missing_credentials_reason() resolves them, so it is also the first chance to register."""
    import boto3

    monkeypatch.setattr(boto3.Session, "get_credentials", lambda self: credentials())
    assert missing_credentials_reason() is None
    assert "AKIAFAKEKEYID000000" in redact.known_secrets()


def test_unavailable_credential_provider_returns_a_reason_without_leaking_details(monkeypatch):
    import boto3
    from botocore.exceptions import CredentialRetrievalError

    def unavailable(self):
        raise CredentialRetrievalError(provider="test", error_msg="private-credential-detail")

    monkeypatch.setattr(boto3.Session, "get_credentials", unavailable)
    reason = missing_credentials_reason()
    assert "CredentialRetrievalError" in reason
    assert "private-credential-detail" not in reason


def verify(tmp_path, fleet, store, *extra):
    profile = tmp_path / "fake.yaml"
    profile.write_text(profile_yaml(fleet.base_url, fleet.model))
    run_dir = tmp_path / "run"
    result = run_llmdv(
        "verify", "--profile", str(profile), "--local-output", str(run_dir),
        "--pod-logs", "off", "--remote-output", "s3://results/nightly", *extra,
        env=s3_env(store),
    )  # fmt: skip
    return result, run_dir, json.loads((run_dir / "outcome.json").read_text())


def test_a_run_is_published_under_its_run_id_with_the_manifest_last(tmp_path):
    with FakeFleet() as fleet, FakeS3() as store:
        result, run_dir, outcome = verify(tmp_path, fleet, store, "-k", "discovery")

    assert result.returncode == 0, result.stderr
    prefix = f"results/nightly/fake/{outcome['run_id']}/"
    assert store.order[0] == "results/nightly/.llmdv-write-check"
    published = store.order[1:]
    assert published and all(key.startswith(prefix) for key in published)
    assert published[-1] == prefix + "MANIFEST.json"

    manifest = json.loads(store.objects[prefix + "MANIFEST.json"])
    assert {entry["path"] for entry in manifest["files"]} >= {
        "outcome.json", "results.jsonl", "llmdv.log", "env.json", "summary.md",
    }  # fmt: skip
    for entry in manifest["files"]:
        uploaded = store.objects[prefix + entry["path"]]
        assert hashlib.sha256(uploaded).hexdigest() == entry["sha256"], entry["path"]

    assert outcome["publication"]["status"] == "published"
    assert outcome["publication"]["location"] == f"s3://{prefix}"

    # The bundle was frozen: published files equal local ones. outcome.json differs by design.
    for entry in manifest["files"]:
        if entry["path"] != "outcome.json":
            local = (run_dir / entry["path"]).read_bytes()
            assert local == store.objects[prefix + entry["path"]], entry["path"]


def test_an_upload_failure_after_passing_tests_exits_not_delivered(tmp_path):
    with FakeFleet() as fleet, FakeS3(deny_after=3) as store:
        result, _, outcome = verify(tmp_path, fleet, store, "-k", "discovery")

    assert result.returncode == 5
    assert outcome["verification"]["class"] == "ok"
    assert outcome["publication"]["status"] == "failed"
    assert not any(key.endswith("MANIFEST.json") for key in store.order)
    assert "NOT published" in result.stderr


def test_a_test_failure_is_never_masked_by_an_upload_failure(tmp_path):
    """Precedence: tests failed AND upload failed exits 1, with both facts recorded."""
    with FakeFleet(abort_finish_reason=True) as fleet, FakeS3(deny_after=3) as store:
        result, _, outcome = verify(tmp_path, fleet, store, "-k", "test_chat_basic and not sdk")

    assert result.returncode == 1
    assert outcome["verification"]["class"] == "fleet"
    assert outcome["publication"]["status"] == "failed"


def test_an_unwritable_bucket_fails_preflight_before_anything_runs(tmp_path):
    with FakeFleet() as fleet, FakeS3(deny_after=0) as store:
        result, _, outcome = verify(tmp_path, fleet, store)

    assert result.returncode == 3
    assert outcome["verification"]["class"] == "environment"
    assert any("not writable" in problem for problem in outcome["preflight_problems"])


def test_artifact_sink_objects_are_not_manifest_files(tmp_path):
    with FakeFleet() as fleet, FakeS3() as store:
        profile = tmp_path / "fake.yaml"
        profile.write_text(profile_yaml(fleet.base_url, fleet.model))
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify",
            "--profile",
            str(profile),
            "--local-output",
            str(run_dir),
            "--pod-logs",
            "off",
            "--transcript-cap",
            "300",
            "--evidence",
            "all",
            "--artifact-sink",
            "s3://results/nightly",
            "--remote-output",
            "s3://results/nightly",
            "-k",
            "streaming_sse",
            env=s3_env(store),
        )
        outcome = json.loads((run_dir / "outcome.json").read_text())

    assert result.returncode == 0, result.stderr
    prefix = f"results/nightly/fake/{outcome['run_id']}/"
    offloaded = [key for key in store.objects if "/offloaded/transcripts/" in key]
    assert offloaded and all(key.startswith(prefix) for key in offloaded)
    manifest = json.loads(store.objects[prefix + "MANIFEST.json"])
    assert not any("offloaded/" in entry["path"] for entry in manifest["files"])
    assert outcome["verification"]["partial_evidence"] == 0


def test_unwritable_artifact_sink_is_an_artifact_preflight_problem(tmp_path):
    with FakeFleet() as fleet, FakeS3(deny_after=0) as store:
        profile = tmp_path / "fake.yaml"
        profile.write_text(profile_yaml(fleet.base_url, fleet.model))
        result = run_llmdv(
            "verify",
            "--profile",
            str(profile),
            "--local-output",
            str(tmp_path / "run"),
            "--pod-logs",
            "off",
            "--artifact-sink",
            "s3://results/nightly",
            env=s3_env(store),
        )
        outcome = json.loads((tmp_path / "run" / "outcome.json").read_text())

    assert result.returncode == 3
    assert any("artifact_sink" in problem for problem in outcome["preflight_problems"])


def test_a_large_artifact_is_hashed_correctly_across_chunks(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    content = bytes(range(256)) * (3 * 4096 + 7)  # just over 3 MiB: several 1 MiB chunks
    (run_dir / "big.bin").write_bytes(content)

    sink_for(f"file://{tmp_path / 'published'}").publish(run_dir, "p/r")

    entry = json.loads((run_dir / "MANIFEST.json").read_text())["files"][0]
    assert entry == {
        "path": "big.bin", "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
    }  # fmt: skip


def test_file_publication_writes_the_same_manifest_last(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "results.jsonl").write_text("{}\n")
    (run_dir / "logs" / "pod.log").write_text("line\n")

    location = sink_for(f"file://{tmp_path / 'published'}").publish(run_dir, "profile/run-1")

    published = tmp_path / "published" / "profile" / "run-1"
    assert location == str(published)
    manifest = json.loads((published / "MANIFEST.json").read_text())
    assert [entry["path"] for entry in manifest["files"]] == ["logs/pod.log", "results.jsonl"]
