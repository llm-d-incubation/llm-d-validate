"""`llmdv job` end to end: real processes against the fake fleet, S3 store and Slack webhook."""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter

import pytest

from llmdv.output import manifest
from tests.fakes import SRC, FakeFleet, FakeS3, FakeSlack, profile_yaml, s3_env

pytest.importorskip("boto3", reason="the s3 extra is not installed")

URI = "s3://results/nightly"


def job_command(tmp_path, fleet, *extra):
    profile = tmp_path / "fake.yaml"
    profile.write_text(profile_yaml(fleet.base_url, fleet.model))
    return [
        sys.executable, "-m", "llmdv.cli.main", "job",
        "--profile", str(profile), "--work-dir", str(tmp_path / "work"),
        "--remote-output", URI, "--pod-logs", "off", *extra,
    ]  # fmt: skip


def job_env(store, slack):
    return {
        "PYTHONPATH": str(SRC),
        "PATH": "/usr/bin:/bin",
        "LLMDV_NOTIFY_WEBHOOK": f"{slack.base_url}/services/x",
        **s3_env(store),
    }


def diagnose(result) -> str:
    """What the job and its verify child said, for an exit code the test did not expect.

    A bare `assert 4 == 5` is how a flake stays a mystery (#105): the child's traceback is in
    the captured stderr, and only the assertion message survives into the CI log.
    """
    lines = (result.stderr or "").splitlines()
    tail = lines[-60:]
    return f"exit {result.returncode}; stderr tail:\n" + "\n".join(tail)


def run_job(tmp_path, fleet, store, slack, *extra, env=None):
    result = subprocess.run(  # noqa: S603 - fixed interpreter and args
        job_command(tmp_path, fleet, *extra),
        capture_output=True,
        text=True,
        cwd=SRC.parent,
        env=job_env(store, slack) if env is None else env,
    )
    return result, outcome_of(tmp_path)


def outcome_of(tmp_path):
    runs = sorted((tmp_path / "work").glob("*/outcome.json"))
    return json.loads(runs[-1].read_text()) if runs else None


def manifests(store):
    return [key for key in store.order if key.endswith("/MANIFEST.json")]


def test_a_passing_run_is_published_and_notified_with_the_callers_selection(tmp_path):
    with FakeFleet() as fleet, FakeS3() as store, FakeSlack() as slack:
        result, outcome = run_job(
            tmp_path, fleet, store, slack, "-k", "models_lists", "--label", "trigger=rollout"
        )

    assert result.returncode == 0, diagnose(result)
    assert outcome["verification"]["class"] == "ok"
    assert outcome["verification"]["counts"] == {"pass": 1}
    # The caller's label overrides the job's default trigger=nightly.
    assert outcome["labels"] == {"trigger": "rollout"}
    assert len(manifests(store)) == 1
    assert slack.received[0]["text"].startswith("llmdv fake: PASSED")
    # The outcome is also in the job log, the copy that outlives the pod.
    assert '"run_id"' in result.stdout


def without_aws_credentials(env):
    """No developer credentials, shared files, or cloud metadata can satisfy this lookup."""
    return {
        key: value
        for key, value in env.items()
        if key not in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
    } | {"AWS_EC2_METADATA_DISABLED": "true"}


@pytest.mark.parametrize(
    "partial", [{}, {"AWS_ACCESS_KEY_ID": "llmdv-test"}, {"AWS_SECRET_ACCESS_KEY": "llmdv-secret"}]
)
def test_missing_or_partial_aws_credentials_skip_s3_but_run_and_notify(tmp_path, partial):
    with FakeFleet() as fleet, FakeS3() as store, FakeSlack() as slack:
        env = without_aws_credentials(job_env(store, slack)) | partial
        result, outcome = run_job(tmp_path, fleet, store, slack, "-k", "models_lists", env=env)

    assert result.returncode == 0, diagnose(result)
    assert outcome["verification"]["counts"] == {"pass": 1}
    assert outcome["publication"]["status"] == "skipped"
    assert outcome["publication"]["destination"] == URI
    assert "credentials" in outcome["publication"]["reason"]
    assert store.order == []  # no S3 preflight write and no fallback upload
    assert "S3 publication skipped" in result.stderr
    assert "publication skipped" in slack.received[0]["text"]
    assert "no --remote-output" not in slack.received[0]["text"]


@pytest.mark.parametrize("fleet_failure", [False, True])
def test_both_integrations_can_be_absent_without_changing_the_test_verdict(tmp_path, fleet_failure):
    with (
        FakeFleet(abort_finish_reason=fleet_failure) as fleet,
        FakeS3() as store,
        FakeSlack() as slack,
    ):
        env = without_aws_credentials(job_env(store, slack))
        env.pop("LLMDV_NOTIFY_WEBHOOK")
        result, outcome = run_job(
            tmp_path, fleet, store, slack, "-k", "test_chat_basic and not sdk", env=env
        )

    assert result.returncode == (1 if fleet_failure else 0), result.stderr
    assert outcome["verification"]["counts"] == {"fail" if fleet_failure else "pass": 1}
    assert outcome["publication"]["status"] == "skipped"
    assert store.order == [] and slack.received == []
    assert "notification skipped" in result.stderr
    assert list((tmp_path / "work").glob("*/results.jsonl"))
    assert '"status": "skipped"' in result.stdout


@pytest.mark.parametrize("webhook", [None, "", "   "])
def test_missing_or_empty_webhook_does_not_stop_publication(tmp_path, webhook):
    with FakeFleet() as fleet, FakeS3() as store, FakeSlack() as slack:
        env = job_env(store, slack)
        env.pop("LLMDV_NOTIFY_WEBHOOK")
        if webhook is not None:
            env["LLMDV_NOTIFY_WEBHOOK"] = webhook
        result, outcome = run_job(tmp_path, fleet, store, slack, "-k", "models_lists", env=env)

    assert result.returncode == 0, diagnose(result)
    assert outcome["publication"]["status"] == "published"
    assert len(manifests(store)) == 1 and slack.received == []
    assert "notification skipped" in result.stderr


def test_shared_file_credentials_work_without_aws_key_environment_variables(tmp_path):
    credentials = tmp_path / "aws-credentials"
    credentials.write_text(
        "[default]\naws_access_key_id = llmdv-test\naws_secret_access_key = llmdv-test-secret\n"
    )
    with FakeFleet() as fleet, FakeS3() as store, FakeSlack() as slack:
        env = without_aws_credentials(job_env(store, slack))
        env["AWS_SHARED_CREDENTIALS_FILE"] = str(credentials)
        result, outcome = run_job(tmp_path, fleet, store, slack, "-k", "models_lists", env=env)

    assert result.returncode == 0, diagnose(result)
    assert outcome["publication"]["status"] == "published"
    assert len(manifests(store)) == 1
    assert "publication skipped" not in result.stderr


def test_file_publication_does_not_require_aws_credentials(tmp_path):
    with FakeFleet() as fleet, FakeS3() as store, FakeSlack() as slack:
        result, outcome = run_job(
            tmp_path,
            fleet,
            store,
            slack,
            "-k",
            "models_lists",
            "--remote-output",
            (tmp_path / "published").as_uri(),
            env=without_aws_credentials(job_env(store, slack)),
        )

    assert result.returncode == 0, diagnose(result)
    assert outcome["publication"]["status"] == "published"
    assert list((tmp_path / "published").glob("fake/*/MANIFEST.json"))


def test_a_bucket_rejecting_configured_credentials_is_not_treated_as_missing(tmp_path):
    with FakeFleet() as fleet, FakeS3(deny_after=0) as store, FakeSlack() as slack:
        result, outcome = run_job(tmp_path, fleet, store, slack, "-k", "models_lists")

    assert result.returncode == 3, diagnose(result)
    assert outcome["verification"]["class"] == "environment"
    assert outcome["publication"]["status"] == "failed"
    assert "publication skipped" not in result.stderr


def test_a_failed_upload_recovered_by_the_fallback_is_a_success(tmp_path):
    with FakeFleet() as fleet, FakeS3(deny_once=("/summary.md",)) as store, FakeSlack() as slack:
        result, outcome = run_job(tmp_path, fleet, store, slack, "-k", "models_lists")

    assert result.returncode == 0, diagnose(result)
    assert outcome["publication"]["status"] == "published"
    assert len(manifests(store)) == 1


def test_a_test_failure_wins_over_a_failed_upload_and_both_are_reported(tmp_path):
    with (
        FakeFleet(abort_finish_reason=True) as fleet,
        FakeS3(deny_after=3) as store,
        FakeSlack() as slack,
    ):
        result, _ = run_job(tmp_path, fleet, store, slack, "-k", "test_chat_basic and not sdk")

    assert result.returncode == 1, diagnose(result)
    text = slack.received[0]["text"]
    assert text.startswith("llmdv fake: FAILED") and "results NOT published" in text


def test_the_run_budget_stops_the_run_and_still_publishes_it(tmp_path):
    with FakeFleet(delay_s=1.0) as fleet, FakeS3() as store, FakeSlack() as slack:
        result, outcome = run_job(tmp_path, fleet, store, slack, "--budget", "4", "-k", "unhappy")

    assert result.returncode == 6, diagnose(result)
    assert outcome["verification"]["class"] == "interrupted"
    # The budget includes collection/setup, so a busy host can stop before the first result.
    assert "the run budget of 4s is spent; stopping the run" in result.stdout + result.stderr
    assert len(manifests(store)) == 1
    assert slack.received[0]["text"].startswith("llmdv fake: INTERRUPTED")


def start_slow_job(tmp_path, fleet, store, slack):
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and args
        job_command(tmp_path, fleet, "-k", "unhappy"),
        cwd=SRC.parent,
        env=job_env(store, slack),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        results = list((tmp_path / "work").glob("*/results.jsonl"))
        if results:
            # A skipped case can finish before any request. Wait for an actual verdict.
            rows = [json.loads(line) for line in results[0].read_text().splitlines()]
            if any(row["outcome"] in ("pass", "fail") for row in rows):
                return process
        if process.poll() is not None:
            pytest.fail("the slow job exited before producing a case verdict")
        time.sleep(0.1)
    process.terminate()
    process.wait(timeout=10)
    pytest.fail("the slow job produced no case verdict within 60 seconds")


def test_sigterm_to_the_job_stops_verify_which_publishes_and_notifies(tmp_path):
    """The budget and Kubernetes both send SIGTERM; completed cases must survive it."""
    with FakeFleet(delay_s=1.0) as fleet, FakeS3() as store, FakeSlack() as slack:
        process = start_slow_job(tmp_path, fleet, store, slack)
        process.send_signal(signal.SIGTERM)
        code = process.wait(timeout=60)

    assert code == 6
    outcome = outcome_of(tmp_path)
    assert outcome["verification"]["class"] == "interrupted"
    assert len(manifests(store)) == 1
    assert len(slack.received) == 1
    local_results = next((tmp_path / "work").glob("*/results.jsonl")).read_bytes()
    uploaded_results = next(v for k, v in store.objects.items() if k.endswith("/results.jsonl"))
    assert uploaded_results == local_results
    rows = [json.loads(line) for line in uploaded_results.splitlines()]
    assert any(row["outcome"] in ("pass", "fail") for row in rows)
    counts = Counter(row["outcome"] for row in rows)
    assert outcome["verification"]["counts"] == counts
    uploaded_outcome = next(v for k, v in store.objects.items() if k.endswith("/outcome.json"))
    assert json.loads(uploaded_outcome)["verification"]["counts"] == counts


@pytest.mark.skipif(shutil.which("pgrep") is None, reason="needs pgrep to find the child")
def test_a_killed_verify_is_still_published_and_notified_as_crashed(tmp_path):
    """An OOM kill of verify: the job outlives it and uploads what it left."""
    with FakeFleet(delay_s=1.0) as fleet, FakeS3() as store, FakeSlack() as slack:
        process = start_slow_job(tmp_path, fleet, store, slack)
        child = subprocess.run(  # noqa: S603, S607 - fixed command
            ["pgrep", "-P", str(process.pid)], capture_output=True, text=True
        ).stdout.split()[0]
        subprocess.run(["kill", "-9", child], check=True)  # noqa: S603, S607
        code = process.wait(timeout=60)

    assert code == 137
    assert outcome_of(tmp_path)["verification"]["class"] == "crashed"
    assert len(manifests(store)) == 1
    assert slack.received[0]["text"].startswith("llmdv fake: CRASHED")


@pytest.mark.parametrize("arg", ["--local-output=/tmp/x", "--local", "--remote=s3://b/p"])
def test_options_the_job_owns_cannot_be_passed_through(tmp_path, arg):
    """One source per setting, so verify and the fallback publish can never disagree."""
    with FakeFleet() as fleet, FakeS3() as store, FakeSlack() as slack:
        result, outcome = run_job(tmp_path, fleet, store, slack, arg)

    assert result.returncode == 2, diagnose(result)
    assert outcome is None and store.order == [] and slack.received == []


def fallback_manifests(root):
    return sorted(root.glob("*/*/MANIFEST.json"))


def test_an_unreachable_s3_falls_back_to_the_pvc(tmp_path):
    """Every upload refused after the preflight check: the bundle lands on the fallback."""
    pvc = tmp_path / "pvc"
    with FakeFleet() as fleet, FakeS3(deny_after=1) as store, FakeSlack() as slack:
        result, outcome = run_job(
            tmp_path,
            fleet,
            store,
            slack,
            "-k",
            "models_lists",
            "--fallback-output",
            f"file://{pvc}",
        )

    assert result.returncode == 0, diagnose(result)
    assert len(fallback_manifests(pvc)) == 1
    publication = outcome["publication"]
    assert publication["status"] == "published" and publication["location"].startswith(str(pvc))
    assert publication["primary"]["destination"] == URI
    assert "denied" in publication["primary"]["error"].lower()
    assert "saved to the fallback" in slack.received[0]["text"]


def test_missing_credentials_fall_back_to_the_pvc(tmp_path):
    pvc = tmp_path / "pvc"
    with FakeFleet() as fleet, FakeS3() as store, FakeSlack() as slack:
        env = without_aws_credentials(job_env(store, slack))
        result, outcome = run_job(
            tmp_path, fleet, store, slack, "-k", "models_lists",
            "--fallback-output", f"file://{pvc}", env=env,
        )  # fmt: skip

    assert result.returncode == 0, diagnose(result)
    assert store.order == []
    assert len(fallback_manifests(pvc)) == 1
    assert "credentials" in outcome["publication"]["primary"]["error"]


def test_the_fallback_is_untouched_while_s3_works(tmp_path):
    pvc = tmp_path / "pvc"
    with FakeFleet() as fleet, FakeS3() as store, FakeSlack() as slack:
        result, outcome = run_job(
            tmp_path,
            fleet,
            store,
            slack,
            "-k",
            "models_lists",
            "--fallback-output",
            f"file://{pvc}",
        )

    assert result.returncode == 0, diagnose(result)
    assert not pvc.exists()
    assert "primary" not in outcome["publication"]


def test_a_failing_fallback_is_still_not_delivered(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    with FakeFleet() as fleet, FakeS3(deny_after=1) as store, FakeSlack() as slack:
        result, outcome = run_job(
            tmp_path, fleet, store, slack, "-k", "models_lists",
            "--fallback-output", f"file://{blocker}/pvc",
        )  # fmt: skip

    assert result.returncode == 5, diagnose(result)
    assert outcome["publication"]["status"] == "failed"
    assert outcome["publication"]["primary"]["destination"] == URI


@pytest.mark.parametrize("fleet_failure", [False, True])
@pytest.mark.parametrize("primary_fails", [False, True])
def test_a_malformed_baseline_is_reported_in_publication_and_notification(
    tmp_path, fleet_failure, primary_fails
):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "results.jsonl").write_text(
        json.dumps(
            {
                "case_id": "openai_chat.basic_non_streaming",
                "ingress": "fake",
                "surface": "chat_completions",
            }
        )
        + "\n"
    )
    # Only a nightly run is a baseline, so label it as one; otherwise compare skips it and
    # never reaches the malformed line this test is about.
    (baseline / "outcome.json").write_text(json.dumps({"labels": {"trigger": "nightly"}}))
    manifest.write(baseline)
    fallback = tmp_path / "fallback"
    with (
        FakeFleet(abort_finish_reason=fleet_failure) as fleet,
        FakeS3(deny_keys=("/summary.md",) if primary_fails else ()) as store,
        FakeSlack() as slack,
    ):
        store.objects.update(
            {
                f"results/nightly/fake/2026-01-01T00Z/{p.name}": p.read_bytes()
                for p in baseline.iterdir()
            }
        )
        result, outcome = run_job(
            tmp_path,
            fleet,
            store,
            slack,
            "-k",
            "test_chat_basic and not sdk",
            "--compare",
            "--fallback-output",
            fallback.as_uri(),
        )

    assert result.returncode == (1 if fleet_failure else 0), result.stderr
    assert "no comparison made" in result.stderr
    assert outcome["publication"]["status"] == "published"
    if primary_fails:
        assert outcome["publication"]["primary"]["destination"] == URI
        bundle = fallback / "fake" / outcome["run_id"]
        assert (bundle / "MANIFEST.json").is_file()
        comparison = (bundle / "comparison.md").read_text()
    else:
        assert not fallback.exists()
        comparison = store.objects[
            f"results/nightly/fake/{outcome['run_id']}/comparison.md"
        ].decode()
    assert comparison.startswith("Comparison unavailable:")
    assert "run 2026-01-01T00Z, line 1" in comparison
    assert len(slack.received) == 1
    assert comparison.strip() in slack.received[0]["text"]
    assert slack.received[0]["text"].startswith(
        "llmdv fake: FAILED" if fleet_failure else "llmdv fake: PASSED"
    )


def test_job_analyze_dashboard_carries_alerts_and_the_comparison(tmp_path):
    """The job writes alerts.json and comparison.json after verify has published, so the
    dashboard must be built after them and follow the run to storage on its own."""
    from tests.test_dashboard import payload

    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "results.jsonl").write_text(
        json.dumps({"case_id": "discovery.models_lists_declared_model", "ingress": "fake",
                    "surface": None, "outcome": "pass"}) + "\n"
    )  # fmt: skip
    (baseline / "outcome.json").write_text(json.dumps({"labels": {"trigger": "nightly"}}))
    manifest.write(baseline)
    with FakeFleet() as fleet, FakeS3() as store, FakeSlack() as slack:
        store.objects.update(
            {
                f"results/nightly/fake/2026-01-01T00Z/{p.name}": p.read_bytes()
                for p in baseline.iterdir()
            }
        )
        result, outcome = run_job(
            tmp_path, fleet, store, slack, "-k", "models_lists", "--compare", "--analyze"
        )

    assert result.returncode == 0, diagnose(result)
    assert outcome["publication"]["status"] == "published"
    run_dir = next((tmp_path / "work").glob("*/outcome.json")).parent
    local = payload(run_dir / "dashboard.html")
    assert local["alerts"] is not None
    assert local["comparison"] is not None and local["comparison"]["made"] is True

    remote = tmp_path / "remote-dashboard.html"
    remote.write_bytes(store.objects[f"results/nightly/fake/{outcome['run_id']}/dashboard.html"])
    published = payload(remote)
    assert published["alerts"] == local["alerts"]
    assert published["comparison"] == local["comparison"]
