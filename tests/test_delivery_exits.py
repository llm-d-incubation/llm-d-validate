"""The delivery commands blame the right party: `publish`, `notify` and `compare` exit 2 for what
they were given, 3 for the operator's environment, and never let a delivery failure replace a
verification verdict."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from llmdv.cli import publish_cmd
from llmdv.cli.exit_codes import INTERRUPTED, NOT_DELIVERED, OK, PREFLIGHT, USAGE
from llmdv.cli.main import main
from llmdv.evidence import outcome as outcome_file
from llmdv.output import manifest
from llmdv.results import read as results_file
from tests.fakes import FakeIAM, FakeS3, FakeSlack, ibm_cos_env, run_llmdv, s3_env

needs_permissions = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes"
)


def result(case="suite.case", outcome="pass", run_id="r"):
    return {"run_id": run_id, "case_id": case, "outcome": outcome, "ingress": "gateway"}


def write_run(run: Path, rows, verification=None, run_id="r") -> Path:
    """A run directory: results, and an outcome that records `verification` (none: crashed)."""
    run.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    (run / "results.jsonl").write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    body = {
        "schema": outcome_file.SCHEMA,
        "run_id": run_id,
        "profile": {"name": "p"},
        "verification": verification or {"exit_code": None, "class": None, "counts": {}},
    }
    (run / "outcome.json").write_text(json.dumps(body), encoding="utf-8")
    return run


def archive(root: Path, *run_ids: str) -> str:
    """A file:// archive of complete runs of profile `p`."""
    for run_id in run_ids:
        run = write_run(root / "p" / run_id, [result(run_id=run_id)], run_id=run_id)
        manifest.write(run)
    return f"file://{root}"


def unwritable(tmp_path: Path) -> str:
    """A file:// destination below a regular file, so nothing can be created there."""
    (tmp_path / "blocker").write_text("a file, not a directory")
    return f"file://{tmp_path / 'blocker' / 'store'}"


PASSED = {"exit_code": 0, "class": "ok", "counts": {"pass": 1}}
FAILED = {"exit_code": 1, "class": "fleet", "counts": {"fail": 1}}


# ----------------------------------------------------------------- publish: whose failure it is


def test_a_failed_upload_never_replaces_a_failed_verification(tmp_path):
    run = write_run(tmp_path / "run", [result(outcome="fail")], FAILED)
    assert main(["publish", str(run), "--remote-output", unwritable(tmp_path)]) == 1
    assert outcome_file.read(run)["publication"]["status"] == "failed"


def test_a_crashed_run_that_cannot_be_published_keeps_its_exit_code(tmp_path):
    run = write_run(tmp_path / "run", [result()])
    args = ["publish", str(run), "--remote-output", unwritable(tmp_path), "--exit-code", "137"]
    assert main(args) == 137
    body = outcome_file.read(run)
    assert body["verification"]["class"] == "crashed"
    assert body["publication"]["status"] == "failed"


def test_publishing_a_passed_run_to_an_unusable_store_is_the_environment(tmp_path):
    """As `llmdv verify` says at preflight for the same store: publish verified nothing itself."""
    run = write_run(tmp_path / "run", [result()], PASSED)
    assert main(["publish", str(run), "--remote-output", unwritable(tmp_path)]) == PREFLIGHT
    assert outcome_file.read(run)["publication"]["status"] == "failed"


def test_a_passed_run_whose_upload_fails_partway_is_not_delivered(tmp_path):
    pytest.importorskip("boto3", reason="the s3 extra is not installed")
    run = write_run(tmp_path / "run", [result()], PASSED)
    # The write check is accepted, so the store is usable; the upload is then refused.
    with FakeS3(deny_after=1) as store:
        done = run_llmdv(
            "publish", str(run), "--remote-output", "s3://results/n", env=s3_env(store)
        )
    assert done.returncode == NOT_DELIVERED, done.stderr


def test_the_job_treats_any_failed_publication_alike(tmp_path):
    """`publish_run` reports the verification's code; `llmdv job` only asks whether it is OK."""
    run = write_run(tmp_path / "run", [result(outcome="fail")], FAILED)
    assert publish_cmd.publish_run(run, unwritable(tmp_path), 1) != OK


# ----------------------------------------------------------------- publish: a second destination


def test_a_second_destination_is_refused_not_silently_skipped(tmp_path):
    run = write_run(tmp_path / "run", [result()], PASSED)
    first, second = f"file://{tmp_path / 'one'}", f"file://{tmp_path / 'two'}"
    assert main(["publish", str(run), "--remote-output", first]) == OK

    done = run_llmdv("publish", str(run), "--remote-output", second)
    assert done.returncode == USAGE, done.stderr
    assert "already published" in done.stderr and str(tmp_path / "one") in done.stderr
    assert not (tmp_path / "two").exists()
    # The same destination again is still a no-op, with or without a trailing slash.
    assert main(["publish", str(run), "--remote-output", first + "/"]) == OK


# ----------------------------------------------------------------- results that are not results


@pytest.mark.parametrize("line", ['"x"', '{"case_id": "c"}', '{"outcome": "maybe"}', "[1]"])
def test_a_line_that_is_not_a_result_is_refused_with_file_and_line(tmp_path, line):
    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps(result()) + "\n" + line + "\n")
    with pytest.raises(results_file.Unreadable, match=rf"{path}, line 2: not a result"):
        results_file.read(path)


def test_a_torn_last_line_is_still_passed_over(tmp_path):
    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps(result()) + '\n{"outcome": "pa')
    assert len(results_file.read(path)) == 1


@pytest.mark.parametrize(
    "line", ['"x"', '{"case_id": "c"}', json.dumps({**result(), "schema": "llmdv.results/3"})]
)
def test_a_crashed_run_with_an_unreadable_line_is_published_and_the_line_reported(tmp_path, line):
    """The evidence still goes out; the line is named and left out of the counts; exit 2."""
    run = write_run(tmp_path / "run", [result(), line, result("suite.other", "fail")])
    done = run_llmdv(
        "publish", str(run), "--remote-output", f"file://{tmp_path / 'store'}",
        "--exit-code", "137",
    )  # fmt: skip

    assert done.returncode == USAGE, done.stderr
    assert "Traceback" not in done.stderr
    assert f"{run / 'results.jsonl'}, line 2" in done.stderr
    published = json.loads((tmp_path / "store" / "p" / "r" / "outcome.json").read_text())
    assert published["verification"]["exit_code"] == 137
    assert published["verification"]["counts"] == {"pass": 1, "fail": 1}
    assert len(published["verification"]["unreadable_results"]) == 1
    assert (tmp_path / "store" / "p" / "r" / "MANIFEST.json").is_file()
    # To the job, the run was delivered.
    again = write_run(tmp_path / "again", [result(), line])
    assert publish_cmd.publish_run(again, f"file://{tmp_path / 'store2'}", 137) == OK


# ----------------------------------------------------------------- a malformed outcome.json


@pytest.mark.parametrize(
    "body",
    [
        {"verification": "x"},
        {"verification": {"counts": [1]}},
        {"verification": {"failures": ["x"]}},
        {"publication": "x"},
        {"profile": []},
    ],
)
def test_a_malformed_outcome_is_named_and_never_a_traceback(tmp_path, body):
    (tmp_path / "outcome.json").write_text(json.dumps({"run_id": "r", **body}))
    with FakeSlack() as slack:
        done = run_llmdv(
            "notify", str(tmp_path), "--exit-code", "1",
            env={"LLMDV_NOTIFY_WEBHOOK": f"{slack.base_url}/services/x"},
        )  # fmt: skip

    assert done.returncode == USAGE, done.stderr
    assert "Traceback" not in done.stderr
    assert str(tmp_path / "outcome.json") in done.stderr
    # The notification still goes: it says the run left no usable outcome, and its exit code.
    assert "exit 1" in slack.received[0]["text"]


def test_publish_rebuilds_a_malformed_outcome_as_it_does_a_truncated_one(tmp_path):
    run = write_run(tmp_path / "run", [result()])
    (run / "outcome.json").write_text(json.dumps({"run_id": "r", "verification": "x"}))
    store = tmp_path / "store"
    assert main(["publish", str(run), "--remote-output", f"file://{store}"]) == OK
    assert list(store.rglob("MANIFEST.json"))


# ----------------------------------------------------------------- compare


@pytest.mark.parametrize("profile", ["../p", ".p", "a/b"])
def test_compare_refuses_a_profile_name_that_leaves_the_archive(tmp_path, profile):
    uri = archive(tmp_path / "arch", "r1", "r2")
    for extra in ([], ["--list"]):
        done = run_llmdv("compare", "--from", uri, "--profile", profile, *extra)
        assert done.returncode == USAGE, done.stderr
        assert "unsafe profile name" in done.stderr and "Traceback" not in done.stderr


@needs_permissions
def test_compare_reports_unreadable_latest_results_as_the_environment(tmp_path):
    uri = archive(tmp_path / "arch", "r1", "r2")
    latest = tmp_path / "arch" / "p" / "r2" / "results.jsonl"
    latest.chmod(0)
    try:
        done = run_llmdv("compare", "--from", uri, "--profile", "p")
    finally:
        latest.chmod(0o644)
    assert done.returncode == PREFLIGHT, done.stderr
    assert "Traceback" not in done.stderr and "p/r2/results.jsonl" in done.stderr


def test_compare_reports_results_that_are_not_text_as_the_environment(tmp_path):
    uri = archive(tmp_path / "arch", "r1", "r2")
    (tmp_path / "arch" / "p" / "r2" / "results.jsonl").write_bytes(b"\xff\xfe\x00 not utf-8\n")
    assert main(["compare", "--from", uri, "--profile", "p"]) == PREFLIGHT


@needs_permissions
def test_compare_reports_an_unreadable_profile_directory_as_the_environment(tmp_path):
    uri = archive(tmp_path / "arch", "r1", "r2")
    directory = tmp_path / "arch" / "p"
    directory.chmod(0)
    try:
        done = run_llmdv("compare", "--from", uri, "--profile", "p", "--list")
    finally:
        directory.chmod(0o755)
    assert done.returncode == PREFLIGHT, done.stderr
    assert "Traceback" not in done.stderr


@needs_permissions
def test_one_unreadable_baseline_is_dropped_but_none_readable_is_the_environment(tmp_path):
    uri = archive(tmp_path / "arch", "r1", "r2", "r3")
    older = tmp_path / "arch" / "p" / "r1" / "results.jsonl"
    older.chmod(0)
    try:
        assert main(["compare", "--from", uri, "--profile", "p", "--window", "2"]) == OK
        assert main(["compare", "--from", uri, "--profile", "p", "--baseline", "r1"]) == (PREFLIGHT)
    finally:
        older.chmod(0o644)


def test_compare_reports_a_rejected_or_unreachable_iam_as_the_environment():
    pytest.importorskip("ibm_boto3", reason="the ibm-cos extra is not installed")
    command = ("compare", "--from", "s3://results/n", "--profile", "p")
    with FakeS3() as store, FakeIAM(reject=True) as iam:
        env = ibm_cos_env(store, iam)
        rejected = run_llmdv(*command, env=env)
        dead = {**env, "IBM_COS_AUTH_ENDPOINT": "http://127.0.0.1:9/identity/token"}
        unreachable = run_llmdv(*command, env=dead)
    for done in (rejected, unreachable):
        assert done.returncode == PREFLIGHT, done.stderr
        assert "Traceback" not in done.stderr
        assert FakeIAM.api_key not in done.stdout + done.stderr


def test_compare_reports_a_store_that_lists_but_refuses_reads_as_the_environment(tmp_path):
    pytest.importorskip("boto3", reason="the s3 extra is not installed")
    with FakeS3(deny_get=("/results.jsonl",), deny_message="denied for llmdv-test") as store:
        for run_id in ("r1", "r2"):
            for name in ("results.jsonl", manifest.FILENAME):
                store.objects[f"results/n/p/{run_id}/{name}"] = (
                    json.dumps(result(run_id=run_id)) + "\n"
                ).encode()
        done = run_llmdv("compare", "--from", "s3://results/n", "--profile", "p", env=s3_env(store))
    assert done.returncode == PREFLIGHT, done.stderr
    assert "p/r2/results.jsonl" in done.stderr
    assert "llmdv-test" not in done.stderr.replace("llmdv-test-secret", "")


# ----------------------------------------------------------------- interrupted


@pytest.mark.parametrize("command", ["publish", "notify", "compare"])
def test_an_interrupt_exits_as_interrupted_for_every_command(tmp_path, monkeypatch, command):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(publish_cmd, "_publish_run", interrupted)
    monkeypatch.setattr("llmdv.cli.notify_cmd.notify_run", interrupted)
    monkeypatch.setattr("llmdv.cli.compare_cmd.list_runs", interrupted)
    argv = {
        "publish": ["publish", str(tmp_path), "--remote-output", f"file://{tmp_path}"],
        "notify": ["notify", str(tmp_path)],
        "compare": ["compare", "--from", f"file://{tmp_path}", "--profile", "p"],
    }[command]
    assert main(argv) == INTERRUPTED
