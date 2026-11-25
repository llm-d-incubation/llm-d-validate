"""A reusable output root holds isolated runs without changing comparison identity."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from llmdv.compare.archive import list_runs
from llmdv.output import local
from tests.fakes import FakeFleet, only_run, profile_yaml, run_llmdv


def test_simultaneous_allocations_never_share_a_directory(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: local.create_run_dir(tmp_path, "example"), range(24)))
    assert len(set(paths)) == 24
    assert all(path.is_dir() and path.parent == tmp_path for path in paths)


def test_a_name_collision_preserves_the_existing_directory(tmp_path, monkeypatch):
    previous = tmp_path / "example-first"
    previous.mkdir()
    evidence = previous / "results.jsonl"
    evidence.write_text("old evidence\n")
    ids = iter(["first", "second"])
    monkeypatch.setattr(local, "new_run_id", lambda: next(ids))

    created = local.create_run_dir(tmp_path, "example")

    assert created == tmp_path / "example-second"
    assert evidence.read_text() == "old evidence\n"
    assert list(created.iterdir()) == []


def test_only_one_child_can_claim_a_job_reservation(tmp_path, monkeypatch):
    # Force both claimants past the emptiness check before either creates the marker.
    original_iterdir = Path.iterdir
    ready = Barrier(2)

    def simultaneous_iterdir(path):
        entries = list(original_iterdir(path))
        ready.wait(timeout=5)
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", simultaneous_iterdir)
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(local.claim_run_dir, tmp_path) for _ in range(2)]
        outcomes = [attempt.exception() for attempt in attempts]
    assert sum(error is None for error in outcomes) == 1
    assert sum(isinstance(error, FileExistsError) for error in outcomes) == 1


def test_a_claimed_job_reservation_cannot_be_reused_before_an_outcome_exists(tmp_path):
    assert local.claim_run_dir(tmp_path) == tmp_path
    with pytest.raises(FileExistsError):
        local.claim_run_dir(tmp_path)


@pytest.mark.parametrize("name", ["../outside", "/absolute/name", "..", "", "a" * 400])
def test_profile_labels_are_bounded_filename_components(tmp_path, name):
    path = local.create_run_dir(tmp_path, name)
    assert path.parent == tmp_path
    assert not path.name.startswith(".")
    assert len(path.name) < 255


@pytest.mark.parametrize("command", ["verify", "job"])
def test_reusing_an_output_root_preserves_runs_and_comparison(tmp_path, command):
    root = tmp_path / "runs"
    root.mkdir()
    (root / "keep.txt").write_text("existing output root contents")
    archive = tmp_path / "archive"
    profile = tmp_path / "profile.yaml"
    flag = "--work-dir" if command == "job" else "--local-output"
    extra = ["--compare"] if command == "job" else []
    args = [
        command, "--profile", str(profile), flag, str(root),
        "--remote-output", archive.as_uri(), "--pod-logs", "off",
        "-k", "test_chat_basic and not sdk", *extra,
    ]  # fmt: skip

    with FakeFleet() as fleet:
        profile.write_text(profile_yaml(fleet.base_url, fleet.model))
        first = run_llmdv(*args)
        assert first.returncode == 0, first.stdout + first.stderr
        first_dir = only_run(root)
        before = {
            path.relative_to(first_dir): path.read_bytes()
            for path in first_dir.rglob("*")
            if path.is_file()
        }
        first_outcome = json.loads((first_dir / "outcome.json").read_text())

        fleet.abort_finish_reason = True
        second = run_llmdv(*args)
        assert second.returncode == 1, second.stdout + second.stderr

    (second_dir,) = {p.parent for p in root.glob("*/outcome.json")} - {first_dir}
    assert before == {
        path.relative_to(first_dir): path.read_bytes()
        for path in first_dir.rglob("*")
        if path.is_file()
    }
    assert (root / "keep.txt").read_text() == "existing output root contents"
    second_outcome = json.loads((second_dir / "outcome.json").read_text())
    ids = [first_outcome["run_id"], second_outcome["run_id"]]
    assert len(set(ids)) == 2
    for directory, run_id, expected in zip(
        (first_dir, second_dir), ids, ("pass", "fail"), strict=True
    ):
        assert re.fullmatch(r"fake-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\dZ-[0-9a-f]+", directory.name)
        rows = [json.loads(line) for line in (directory / "results.jsonl").read_text().splitlines()]
        assert {row["run_id"] for row in rows} == {run_id}
        assert {row["outcome"] for row in rows} == {expected}
        assert (archive / "fake" / run_id / "MANIFEST.json").is_file()
    assert list_runs(archive.as_uri(), "fake") == ids

    compared = run_llmdv("compare", "--from", archive.as_uri(), "--profile", "fake")
    assert compared.returncode == 1, compared.stdout + compared.stderr
    assert "1 newly failing" in compared.stdout.lower()
    assert "openai_chat.basic_non_streaming" in compared.stdout
    if command == "job":
        comparison = json.loads((second_dir / "comparison.json").read_text())
        assert comparison["made"] and comparison["regressed"]
        assert comparison["baseline"] == first_outcome["run_id"]
        assert "--run-dir" not in (second_dir / "reproduce.sh").read_text()


def test_an_unusable_output_root_is_an_environment_error(tmp_path):
    root = tmp_path / "file"
    root.write_text("preserve me")
    result = run_llmdv("verify", "--profile", "missing.yaml", "--local-output", str(root))
    assert result.returncode == 3, result.stderr
    assert root.read_text() == "preserve me"


def test_a_private_job_reservation_cannot_reuse_a_run(tmp_path):
    outcome = tmp_path / "outcome.json"
    outcome.write_text("previous outcome")
    result = run_llmdv("verify", "--profile", "missing.yaml", "--run-dir", str(tmp_path))
    assert result.returncode == 3, result.stderr
    assert outcome.read_text() == "previous outcome"
