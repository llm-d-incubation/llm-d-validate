"""Supporting work can fail without erasing already-flushed verification evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llmdv.cli import verify_cmd
from llmdv.cli.exit_codes import INTERNAL, INTERRUPTED
from llmdv.cli.main import build_parser
from llmdv.results.record import ResultRecord, ResultsWriter
from tests.fakes import profile_yaml


@pytest.mark.parametrize("stage", ["pytest", "collection", "summary"])
@pytest.mark.parametrize(
    "failure,expected", [(RuntimeError, INTERNAL), (KeyboardInterrupt, INTERRUPTED)]
)
def test_finalization_keeps_finished_cases_when_a_later_step_fails(
    tmp_path, monkeypatch, stage, failure, expected
):
    profile = tmp_path / "profile.yaml"
    profile.write_text(profile_yaml("http://127.0.0.1:9", "fake"))
    root = tmp_path / "runs"
    archive = tmp_path / "archive"
    args = build_parser().parse_args(
        [
            "verify", "--profile", str(profile), "--local-output", str(root),
            "--pod-logs", "off", "--skip-preflight", "--remote-output", archive.as_uri(),
        ]
    )  # fmt: skip
    args.pytest_args = []
    monkeypatch.setattr(verify_cmd.signal, "signal", lambda *args: None)
    monkeypatch.setattr(verify_cmd.log_setup, "configure", lambda **kwargs: None)
    monkeypatch.setattr(verify_cmd.log_setup, "close_run_log", lambda: None)

    def fail(*args, **kwargs):
        raise failure("injected failure after a completed case")

    def run_pytest(argv, **kwargs):
        run_dir = Path(argv[argv.index("--llmdv-run-dir") + 1])
        run_id = argv[argv.index("--llmdv-run-id") + 1]
        writer = ResultsWriter(run_dir / "results.jsonl")
        for verdict in ("pass", "fail"):
            writer.append(
                ResultRecord(
                    run_id=run_id,
                    nodeid=f"test_{verdict}",
                    outcome=verdict,
                    profile="fake",
                    case_id=f"fake.{verdict}",
                    ingress="fake",
                    reason="observed failure" if verdict == "fail" else None,
                )
            )
        writer.close()
        if stage == "pytest":
            # A partial last write must not cost the two complete records preceding it.
            with (run_dir / "results.jsonl").open("a") as output:
                output.write('{"outcome":')
            fail()
        return 1

    monkeypatch.setattr(verify_cmd, "_run_pytest", run_pytest)
    monkeypatch.setattr(
        verify_cmd, "_collect_logs", fail if stage == "collection" else lambda *a: None
    )
    if stage == "summary":
        monkeypatch.setattr(verify_cmd, "_summarise", fail)

    assert verify_cmd.cmd_verify(args) == expected
    outcome_path = root / "outcome.json"
    outcome = json.loads(outcome_path.read_text())
    verification = outcome["verification"]
    assert verification["exit_code"] == expected
    assert verification["counts"] == {"pass": 1, "fail": 1}
    assert [item["case_id"] for item in verification["failures"]] == ["fake.fail"]
    assert verification["failures"][0]["reason"] == "observed failure"
    published = archive / "fake" / outcome["run_id"]
    published_outcome = json.loads((published / "outcome.json").read_text())
    assert published_outcome["verification"] == verification
    assert (published / "MANIFEST.json").is_file()
