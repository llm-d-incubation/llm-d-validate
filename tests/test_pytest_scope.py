"""Installed location, caller config and replay profile paths must not rename cases."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.fakes import SRC, FakeFleet, only_run, profile_yaml


def _cli(cwd, source, *args):
    return subprocess.run(
        [sys.executable, "-m", "llmdv.cli.main", *args],
        cwd=cwd,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        timeout=90,
    )


@pytest.mark.parametrize("config", ["pytest.ini", "pyproject.toml", "setup.cfg"])
def test_plan_and_verify_ignore_caller_config_and_keep_nodeids(tmp_path, config):
    caller = tmp_path / "caller"
    caller.mkdir()
    contents = {
        "pytest.ini": "[pytest]\naddopts = --an-unrelated-project-option\n",
        "pyproject.toml": '[tool.pytest.ini_options]\naddopts = "--an-unrelated-project-option"\n',
        "setup.cfg": "[tool:pytest]\naddopts = --an-unrelated-project-option\n",
    }
    (caller / config).write_text(contents[config])
    (caller / "conftest.py").write_text('raise RuntimeError("unrelated conftest loaded")\n')
    # A second import location models a wheel installed below the caller's project.
    installed = caller / "venv" / "site-packages"
    shutil.copytree(
        SRC / "llmdv", installed / "llmdv", ignore=shutil.ignore_patterns("__pycache__")
    )
    nested = caller / "nested"
    nested.mkdir()
    with FakeFleet() as fleet:
        profile = nested / "profile.replay.yaml"
        profile.write_text(profile_yaml(fleet.base_url, fleet.model))
        expected = None
        for cwd, source, path in [
            (tmp_path, SRC, str(profile)),
            (nested, installed, "profile.replay.yaml"),
            (caller, installed, str(profile)),
        ]:
            selection = ["--profile", path, "-k", "discovery"]
            plan = _cli(cwd, source, "plan", *selection, "--json")
            assert plan.returncode == 0, plan.stderr
            rows = json.loads(plan.stdout)["cases"]
            ids = {r["nodeid"] for r in rows}
            assert ids and all(n.startswith("discovery/") for n in ids)
            if expected is None:
                expected = ids
            assert ids == expected
            output = tmp_path / "runs"
            result = _cli(
                cwd,
                source,
                "verify",
                *selection,
                "--skip-preflight",
                "--pod-logs",
                "off",
                "--local-output",
                str(output),
                "--analyze",
            )
            assert result.returncode == 0, result.stderr
            run = only_run(output)
            records = [
                json.loads(line) for line in (run / "results.jsonl").read_text().splitlines()
            ]
            assert {r["nodeid"] for r in records} == expected
            exchanges = [
                json.loads(line) for line in (run / "exchanges.jsonl").read_text().splitlines()
            ]
            assert exchanges and {e["nodeid"] for e in exchanges} <= expected
            assert (run / "dashboard.html").is_file()
            shutil.rmtree(output)
