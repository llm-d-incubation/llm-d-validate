"""Run the installer offline, with pip and the installed commands stubbed."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"

# A hang guard, not a performance bound: the installer itself finishes in well under a second.
# macOS checks each freshly written executable on its first exec, and under `-n auto` load the
# stubs below can take longer than 10 s to start, which failed the test with no defect present.
INSTALL_HANG_GUARD_SECONDS = 60


def _script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _install(tmp_path: Path, flags: list[str], *, ruff: bool, boto3: bool = True):
    installer = tmp_path / "install.sh"
    shutil.copyfile(INSTALLER, installer)
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    _script(bin_dir / "python", "exit 0\n" if boto3 else '[ "$*" != "-c import boto3" ]\n')
    _script(bin_dir / "llmdv", "printf 'llmdv 0.1.0\\n'\n")
    _script(bin_dir / "pip", 'printf "%s\\n" "$@" >> pip-args\n')
    if ruff:
        _script(bin_dir / "ruff", "exit 0\n")

    result = subprocess.run(
        ["bash", str(installer), *flags],
        env={"PATH": os.defpath, "PYTHON": str(bin_dir / "python")},
        capture_output=True,
        text=True,
        timeout=INSTALL_HANG_GUARD_SECONDS,
    )
    return result, (tmp_path / "pip-args").read_text().splitlines()


@pytest.mark.parametrize(
    ("flags", "package", "ruff"),
    [
        ([], ".", False),
        (["--s3"], ".[s3]", False),
        (["--dev"], ".[dev,s3,ibm-cos]", True),
        (["--s3", "--dev"], ".[dev,s3,ibm-cos]", True),
        (["--dev", "--s3"], ".[dev,s3,ibm-cos]", True),
    ],
)
def test_installer_selects_extras_and_only_requires_requested_tools(tmp_path, flags, package, ruff):
    result, pip_args = _install(tmp_path, flags, ruff=ruff, boto3=package != ".")

    assert result.returncode == 0, result.stdout + result.stderr
    assert pip_args[-1] == package
    assert ("boto3 available" in result.stdout) == (package != ".")
    assert ("ruff available" in result.stdout) == ("--dev" in flags)


def test_dev_install_still_fails_when_ruff_is_missing(tmp_path):
    result, _ = _install(tmp_path, ["--dev"], ruff=False)

    assert result.returncode == 1
    assert "ruff was requested but is not available" in result.stderr


@pytest.mark.parametrize("flags", [["--s3"], ["--dev"]])
def test_install_fails_when_requested_boto3_cannot_import(tmp_path, flags):
    result, _ = _install(tmp_path, flags, ruff="--dev" in flags, boto3=False)

    assert result.returncode == 1
    assert "boto3 was requested but is not available" in result.stderr
