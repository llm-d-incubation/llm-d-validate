"""A stopped or crashed run still gets as much of its evidence to storage as it can."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time

import pytest

from tests.fakes import SRC, FakeFleet, FakeS3, profile_yaml, run_llmdv, s3_env

pytest.importorskip("boto3", reason="the s3 extra is not installed")

URI = "s3://results/nightly"


def start_verify(tmp_path, fleet, store):
    """Start a slow verify run, and return it once at least one case has been recorded."""
    profile = tmp_path / "fake.yaml"
    profile.write_text(profile_yaml(fleet.base_url, fleet.model))
    run_dir = tmp_path / "run"
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and args
        [sys.executable, "-m", "llmdv.cli.main", "verify", "--profile", str(profile),
         "--local-output", str(run_dir), "--pod-logs", "off", "--remote-output", URI,
         "-k", "unhappy"],
        cwd=SRC.parent,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin", **s3_env(store)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    results = run_dir / "results.jsonl"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not (results.exists() and results.read_text()):
        time.sleep(0.1)
    return process, run_dir


def published_keys(store, run_dir):
    run_id = json.loads((run_dir / "outcome.json").read_text())["run_id"]
    prefix = f"results/nightly/fake/{run_id}/"
    return prefix, [key for key in store.order if key.startswith(prefix)]


def test_sigterm_stops_the_run_and_still_publishes_it(tmp_path):
    with FakeFleet(delay_s=1.0) as fleet, FakeS3() as store:
        process, run_dir = start_verify(tmp_path, fleet, store)
        process.send_signal(signal.SIGTERM)
        code = process.wait(timeout=60)

    assert code == 6
    outcome = json.loads((run_dir / "outcome.json").read_text())
    assert outcome["verification"]["class"] == "interrupted"
    assert outcome["verification"]["counts"]
    prefix, keys = published_keys(store, run_dir)
    assert keys[-1] == prefix + "MANIFEST.json"


def test_a_killed_run_is_published_by_llmdv_publish_as_crashed(tmp_path):
    """SIGKILL leaves no chance to publish; what the run wrote is still uploaded afterwards."""
    with FakeFleet(delay_s=1.0) as fleet, FakeS3() as store:
        process, run_dir = start_verify(tmp_path, fleet, store)
        process.kill()
        process.wait(timeout=60)

        # outcome.json was written at the start, so the bundle knows its run id and profile.
        assert json.loads((run_dir / "outcome.json").read_text())["verification"]["class"] is None

        result = run_llmdv(
            "publish", str(run_dir), "--remote-output", URI, "--exit-code", "137",
            env=s3_env(store),
        )  # fmt: skip

    assert result.returncode == 0, result.stderr
    prefix, keys = published_keys(store, run_dir)
    assert keys[-1] == prefix + "MANIFEST.json"
    assert {prefix + "llmdv.log", prefix + "results.jsonl", prefix + "outcome.json"} <= set(keys)

    published = json.loads(store.objects[prefix + "outcome.json"])
    assert published["verification"]["exit_code"] == 137
    assert published["verification"]["class"] == "crashed"
    assert sum(published["verification"]["counts"].values()) >= 1


def test_publishing_an_already_published_run_does_nothing(tmp_path):
    with FakeFleet() as fleet, FakeS3() as store:
        profile = tmp_path / "fake.yaml"
        profile.write_text(profile_yaml(fleet.base_url, fleet.model))
        run_dir = tmp_path / "run"
        first = run_llmdv(
            "verify", "--profile", str(profile), "--local-output", str(run_dir),
            "--pod-logs", "off", "--remote-output", URI, "-k", "models_lists",
            env=s3_env(store),
        )  # fmt: skip
        uploads = len(store.order)
        again = run_llmdv(
            "publish", str(run_dir), "--remote-output", URI, "--exit-code", "0",
            env=s3_env(store),
        )  # fmt: skip

    assert first.returncode == 0 and again.returncode == 0
    assert len(store.order) == uploads


def test_one_file_failing_does_not_stop_the_rest(tmp_path):
    with FakeFleet() as fleet, FakeS3(deny_keys=("/summary.md",)) as store:
        profile = tmp_path / "fake.yaml"
        profile.write_text(profile_yaml(fleet.base_url, fleet.model))
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify", "--profile", str(profile), "--local-output", str(run_dir),
            "--pod-logs", "off", "--remote-output", URI, "-k", "models_lists",
            env=s3_env(store),
        )  # fmt: skip

    assert result.returncode == 5
    prefix, keys = published_keys(store, run_dir)
    # Everything else arrived; the manifest is withheld because the copy is incomplete.
    assert {prefix + "results.jsonl", prefix + "llmdv.log", prefix + "outcome.json"} <= set(keys)
    assert prefix + "MANIFEST.json" not in keys
    error = json.loads((run_dir / "outcome.json").read_text())["publication"]["error"]
    assert "1 of" in error and "summary.md" in error


def test_publish_refuses_an_empty_directory(tmp_path):
    result = run_llmdv("publish", str(tmp_path), "--remote-output", "file:///tmp/x")
    assert result.returncode == 2
    assert "nothing to publish" in result.stderr


def test_a_corrupt_outcome_still_publishes_and_notifies(tmp_path):
    """A kill mid-write could once leave outcome.json truncated; recovery must not depend on it."""
    from tests.fakes import FakeSlack

    with FakeFleet() as fleet, FakeS3() as store, FakeSlack() as slack:
        profile = tmp_path / "fake.yaml"
        profile.write_text(profile_yaml(fleet.base_url, fleet.model))
        run_dir = tmp_path / "run"
        run_llmdv(
            "verify", "--profile", str(profile), "--local-output", str(run_dir),
            "--pod-logs", "off", "-k", "models_lists",
        )  # fmt: skip
        run_id = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])["run_id"]
        (run_dir / "outcome.json").write_text('{"run_id": "trunc')

        published = run_llmdv(
            "publish", str(run_dir), "--remote-output", URI, "--exit-code", "137",
            env=s3_env(store),
        )  # fmt: skip
        notified = run_llmdv(
            "notify", str(run_dir), "--exit-code", "137",
            env={"LLMDV_NOTIFY_WEBHOOK": f"{slack.base_url}/services/x"},
        )  # fmt: skip

    assert published.returncode == 0, published.stderr
    # Rebuilt from results.jsonl (run id) and profile.used.yaml (profile name).
    prefix = f"results/nightly/fake/{run_id}/"
    assert prefix + "MANIFEST.json" in store.order
    assert json.loads(store.objects[prefix + "outcome.json"])["verification"]["class"] == "crashed"
    assert notified.returncode == 0, notified.stderr
    assert "exit 137" in slack.received[0]["text"]
