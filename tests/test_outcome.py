"""outcome.json is written, with the right class, however a run ends. Runs the real CLI."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time

from llmdv.cli import verify_cmd
from llmdv.cli.exit_codes import INTERNAL, INTERRUPTED
from llmdv.evidence.outcome import Outcome
from tests.fakes import SRC, FakeFleet, profile_yaml, run_llmdv


def write_profile(tmp_path, fleet, **kw):
    path = tmp_path / "fake.yaml"
    path.write_text(profile_yaml(fleet.base_url, fleet.model, **kw))
    return path


def outcome_of(run_dir):
    return json.loads((run_dir / "outcome.json").read_text())


def test_partial_evidence_is_counted_in_verification():
    outcome = Outcome("run")
    outcome.record_results(
        [
            {"outcome": "pass", "partial_evidence": True},
            {"outcome": "pass", "partial_evidence": False},
        ]
    )

    assert outcome.to_dict()["verification"]["partial_evidence"] == 1


def test_a_passing_run_records_its_outcome_labels_and_one_run_id(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify", "--profile", str(profile), "--local-output", str(run_dir),
            "--pod-logs", "off", "-k", "discovery",
            "--label", "trigger=nightly", "--label", "trigger=rollout", "--label", "rollout=r1",
        )  # fmt: skip

    assert result.returncode == 0, result.stderr
    outcome = outcome_of(run_dir)
    assert outcome["verification"]["class"] == "ok"
    assert outcome["verification"]["exit_code"] == 0
    assert outcome["verification"]["counts"]["pass"] >= 1
    # A later label overrides an earlier one, so a caller can replace the runner's default.
    assert outcome["labels"] == {"trigger": "rollout", "rollout": "r1"}
    assert outcome["publication"] == {"status": "not_requested"}

    results = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    env = json.loads((run_dir / "env.json").read_text())
    assert {r["run_id"] for r in results} == {outcome["run_id"]} == {env["run_id"]}
    assert env["labels"] == outcome["labels"]


def test_a_fleet_failure_is_recorded_with_the_failing_case(tmp_path):
    with FakeFleet(abort_finish_reason=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify", "--profile", str(profile), "--local-output", str(run_dir),
            "--pod-logs", "off", "-k", "test_chat_basic and not sdk",
        )  # fmt: skip

    assert result.returncode == 1
    verification = outcome_of(run_dir)["verification"]
    assert verification["class"] == "fleet"
    assert [f["case_id"] for f in verification["failures"]] == ["openai_chat.basic_non_streaming"]
    assert "abandoned" in verification["failures"][0]["reason"]


def test_an_invalid_profile_still_leaves_a_bundle(tmp_path):
    """The run directory exists before the profile is read, so a refusal is on record too."""
    profile = tmp_path / "broken.yaml"
    profile.write_text(
        "schema: llmdv.profile/1\nname: broken\nmodel: {public_id: m}\ningresses: []\n"
    )
    run_dir = tmp_path / "run"
    result = run_llmdv("verify", "--profile", str(profile), "--local-output", str(run_dir))

    assert result.returncode == 2
    outcome = outcome_of(run_dir)
    assert outcome["verification"]["class"] == "usage"
    assert outcome["profile"]["name"] == "broken"
    assert (run_dir / "llmdv.log").is_file()
    assert (run_dir / "profile.used.yaml").read_text() == profile.read_text()


def test_an_unreadable_profile_still_leaves_a_bundle(tmp_path):
    """A profile that cannot even be parsed is refused on record, not with a bare traceback."""
    profile = tmp_path / "unparseable.yaml"
    profile.write_text("name: [unclosed\n")
    run_dir = tmp_path / "run"
    result = run_llmdv("verify", "--profile", str(profile), "--local-output", str(run_dir))

    assert result.returncode == 2
    outcome = outcome_of(run_dir)
    assert outcome["verification"]["class"] == "usage"
    assert outcome["profile"]["name"] == "unparseable"
    assert "invalid YAML" in (run_dir / "llmdv.log").read_text()


def test_a_malformed_label_is_a_usage_error_on_record(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify", "--profile", str(profile), "--local-output", str(run_dir),
            "--label", "no-equals-sign",
        )  # fmt: skip

    assert result.returncode == 2
    assert outcome_of(run_dir)["verification"]["class"] == "usage"
    assert "key=value" in result.stderr


def test_a_failed_preflight_is_an_environment_outcome_with_its_problems(tmp_path):
    profile = tmp_path / "dead.yaml"
    profile.write_text(profile_yaml("http://127.0.0.1:1"))
    run_dir = tmp_path / "run"
    result = run_llmdv("verify", "--profile", str(profile), "--local-output", str(run_dir))

    assert result.returncode == 3
    outcome = outcome_of(run_dir)
    assert outcome["verification"]["class"] == "environment"
    assert any("reachability" in problem for problem in outcome["preflight_problems"])
    assert json.loads((run_dir / "env.json").read_text())["run_id"] == outcome["run_id"]


def test_a_model_the_fleet_does_not_offer_stops_the_run_in_preflight(tmp_path):
    """Every case would fail on the model, so none runs and the blame is the environment's."""
    with FakeFleet(model="fake/Other-7B") as fleet:
        profile = tmp_path / "elsewhere.yaml"
        profile.write_text(profile_yaml(fleet.base_url))
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify", "--profile", str(profile), "--local-output", str(run_dir),
            "--pod-logs", "off",
        )  # fmt: skip

    assert result.returncode == 3, result.stderr
    outcome = outcome_of(run_dir)
    assert outcome["verification"]["class"] == "environment"
    assert any("access" in problem for problem in outcome["preflight_problems"])
    results = run_dir / "results.jsonl"
    assert not results.exists() or not results.read_text().strip()


def test_sigint_mid_run_is_an_interrupted_outcome_not_a_crash(tmp_path):
    """What the runner's `timeout -s INT` does when the run budget expires."""
    with FakeFleet(delay_s=1.0) as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        process = subprocess.Popen(  # noqa: S603 - fixed interpreter and args
            [sys.executable, "-m", "llmdv.cli.main", "verify", "--profile", str(profile),
             "--local-output", str(run_dir), "--pod-logs", "off", "-k", "unhappy"],
            cwd=SRC.parent,
            env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )  # fmt: skip
        # Wait until a case has finished: results.jsonl is created at startup, so wait for a line.
        results = run_dir / "results.jsonl"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (results.exists() and results.read_text()):
            time.sleep(0.1)
        process.send_signal(signal.SIGINT)
        code = process.wait(timeout=60)

    assert code == INTERRUPTED
    outcome = outcome_of(run_dir)
    assert outcome["verification"]["class"] == "interrupted"
    assert outcome["verification"]["counts"]


def test_a_broken_test_module_is_a_llmdv_defect_not_an_interrupt(tmp_path):
    """pytest reports a collection error as "interrupted"; only a real signal is one."""
    broken = tmp_path / "test_broken.py"
    broken.write_text('raise RuntimeError("broken import")\n')
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify", "--profile", str(profile), "--local-output", str(run_dir),
            "--pod-logs", "off", "--", str(broken),
        )  # fmt: skip

    assert result.returncode == INTERNAL
    assert outcome_of(run_dir)["verification"]["class"] == "internal"


def test_pytest_interrupted_means_the_budget_only_when_sigint_caused_it():
    """pytest reports a collection error as "interrupted" too, and that one is a llmdv defect."""
    assert verify_cmd._map_pytest_exit(2, interrupted=True) == INTERRUPTED
    assert verify_cmd._map_pytest_exit(2, interrupted=False) == INTERNAL
