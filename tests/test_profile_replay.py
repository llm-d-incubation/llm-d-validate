"""Run reproduction uses archived declarations and preserves pytest arguments."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from llmdv.evidence import reproduce
from tests.fakes import SRC, FakeFleet, profile_yaml


@pytest.mark.parametrize(
    "options",
    [
        ["--profile", "old.yaml"],
        ["--profile=old.yaml"],
        ["--profile=old.yaml", "--profile", "new.yaml"],
        ["--profile", "old.yaml", "--profile=new.yaml"],
    ],
)
def test_every_profile_occurrence_uses_the_archived_copy(options):
    rewritten = reproduce.rewrite_argv(["llmdv", "verify", *options], "profile.used.yaml")
    assert "old.yaml" not in " ".join(rewritten)
    assert "new.yaml" not in " ".join(rewritten)
    assert rewritten.count("profile.used.yaml") == sum(
        option.startswith("--profile") for option in options
    )


def test_pytest_arguments_after_the_separator_are_preserved_verbatim():
    tail = ["--", "--profile=pytest-option", "--local-output", "pytest-output", "a b.py"]
    original = ["llmdv", "verify", "--profile=old.yaml", "--local-output=old-run", *tail]
    assert reproduce.rewrite_argv(original, "profile.used.yaml") == [
        "llmdv", "verify", "--profile", "profile.used.yaml", *tail,
    ]  # fmt: skip


@pytest.mark.parametrize("form", ["separate", "equals"])
def test_generated_script_replays_after_the_original_profile_is_moved(tmp_path, form):
    bundle = tmp_path / "copied bundle"
    bundle.mkdir()
    original = tmp_path / "original profile.yaml"
    output = tmp_path / "new results"
    with FakeFleet() as fleet:
        original.write_text(profile_yaml(fleet.base_url, fleet.model))
        archived = bundle / "profile.used.yaml"
        archived.write_text(original.read_text())
        profile_args = (
            ["--profile", str(original)] if form == "separate" else [f"--profile={original}"]
        )
        argv = reproduce.rewrite_argv(
            [sys.executable, "-m", "llmdv.cli.main", "verify", *profile_args,
             "--pod-logs", "off", "--local-output", str(bundle),
             "--", "-k", "models_lists"],
            "profile.used.yaml",
        )  # fmt: skip
        script = reproduce.write(bundle, "fake", None, argv, {}, [])
        original.rename(tmp_path / "moved.yaml")
        result = subprocess.run(  # noqa: S603 -- generated local replay and fake fleet
            ["bash", str(script)],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(SRC), "LLMDV_RERUN_OUT": str(output)},
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    # This regression test also works with the original exact-directory output contract.
    outcomes = list(output.rglob("outcome.json"))
    assert len(outcomes) == 1
    outcome = json.loads(outcomes[0].read_text())
    assert outcome["verification"]["class"] == "ok"
    assert outcome["verification"]["counts"].get("pass", 0) > 0
    assert (outcomes[0].parent / "profile.used.yaml").read_text() == archived.read_text()
    assert not (bundle / "outcome.json").exists()
