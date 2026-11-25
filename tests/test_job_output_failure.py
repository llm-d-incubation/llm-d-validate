"""A job reports output setup failures without reading evidence from an older run."""

from __future__ import annotations

import json

import pytest

from llmdv.cli import job_cmd
from llmdv.cli.main import build_parser
from tests.fakes import FakeSlack, run_llmdv


@pytest.mark.parametrize("status", [200, 400])
def test_unusable_output_root_notifies_and_keeps_environment_exit(tmp_path, status):
    root = tmp_path / "work"
    root.write_text("preserve existing contents")
    with FakeSlack(script=[(status, {})]) as slack:
        result = run_llmdv(
            "job", "--profile", str(tmp_path / "missing.yaml"), "--work-dir", str(root),
            env={"LLMDV_NOTIFY_WEBHOOK": slack.base_url},
        )  # fmt: skip
    assert result.returncode == 3, result.stderr
    assert root.read_text() == "preserve existing contents"
    assert len(slack.received) == 1
    text = slack.received[0]["text"]
    assert "NOT RUN" in text and "exit 3" in text
    assert "before recording an outcome" in text
    if status == 400:
        assert "notification not sent" in result.stderr


def test_failed_reservation_cannot_notify_using_stale_root_evidence(tmp_path, monkeypatch):
    previous = {
        "profile": {"name": "previous-run"},
        "verification": {"class": "ok", "exit_code": 0},
    }
    (tmp_path / "outcome.json").write_text(json.dumps(previous))
    (tmp_path / "comparison.md").write_text("previous comparison")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    def fail(*args):
        raise PermissionError("cannot create child directory")

    monkeypatch.setattr(job_cmd, "create_run_dir", fail)
    args = build_parser().parse_args([
        "job", "--profile", str(tmp_path / "missing.yaml"), "--work-dir", str(tmp_path),
    ])  # fmt: skip
    args.pytest_args = []
    with FakeSlack() as slack:
        monkeypatch.setenv("LLMDV_NOTIFY_WEBHOOK", slack.base_url)
        assert job_cmd.cmd_job(args) == 3
    text = slack.received[0]["text"]
    assert "exit 3" in text
    assert "previous-run" not in text and "previous comparison" not in text
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}
