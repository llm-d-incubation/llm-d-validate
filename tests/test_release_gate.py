"""Release authorization rejects wrong actors, versions and unmerged Git history."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/check-release.sh"


@pytest.fixture
def release_repo(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args, cwd=source):
        return subprocess.check_output(
            ["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()

    git("init", "-b", "main")
    for key, value in {
        "user.name": "Release test",
        "user.email": "release@example.invalid",
        "commit.gpgsign": "false",
        "tag.gpgsign": "false",
        "core.hooksPath": "/dev/null",
    }.items():
        git("config", key, value)
    (source / "src/llmdv").mkdir(parents=True)
    (source / "src/llmdv/__init__.py").write_text('__version__ = "1.2.3"\n')
    gate = source / ".github/scripts/check-release.sh"
    gate.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, gate)
    git("add", ".")
    git("commit", "-m", "merged release")
    git("tag", "-a", "v1.2.3", "-m", "release")
    git("switch", "-c", "feature")
    (source / "feature").write_text("unmerged\n")
    git("add", ".")
    git("commit", "-m", "unmerged feature")
    feature = git("rev-parse", "HEAD")
    git("switch", "main")
    git("commit", "--allow-empty", "-m", "main advances")
    checkout = tmp_path / "checkout"
    git("clone", str(source), str(checkout))
    git("checkout", "--detach", "v1.2.3", cwd=checkout)
    binary = tmp_path / "bin"
    binary.mkdir()
    gh = binary / "gh"
    gh.write_text('#!/bin/sh\necho "$TEST_PERMISSION"\nexit "${TEST_API_EXIT:-0}"\n')
    gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{binary}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "example/repo",
        "GITHUB_ACTOR": "release-admin",
        "GITHUB_REF_NAME": "v1.2.3",
        "TEST_PERMISSION": "admin",
    }
    return checkout, git, feature, env


@pytest.mark.parametrize(
    "scenario,allowed",
    [
        ("older-main-tag", True),
        ("main-tip", True),
        ("non-admin", False),
        ("api-error", False),
        ("wrong-version", False),
        ("unmerged", False),
        ("squashed-feature", False),
        ("fetch-error", False),
    ],
)
def test_release_gate(release_repo, scenario, allowed):
    checkout, git, feature, env = release_repo
    if scenario == "main-tip":
        git("checkout", "--detach", "origin/main", cwd=checkout)
    elif scenario == "non-admin":
        env["TEST_PERMISSION"] = "write"
    elif scenario == "api-error":
        env["TEST_API_EXIT"] = "1"
    elif scenario == "wrong-version":
        env["GITHUB_REF_NAME"] = "v9.9.9"
    elif scenario in {"unmerged", "squashed-feature"}:
        if scenario == "squashed-feature":
            git("merge", "--squash", "feature")
            git("commit", "-m", "squashed feature")
        git("checkout", "--detach", feature, cwd=checkout)
    elif scenario == "fetch-error":
        git("remote", "set-url", "origin", str(checkout / "missing"), cwd=checkout)
    result = subprocess.run(
        ["bash", ".github/scripts/check-release.sh"],
        cwd=checkout,
        env=env,
        text=True,
        capture_output=True,
    )
    assert (result.returncode == 0) == allowed, result.stdout + result.stderr
