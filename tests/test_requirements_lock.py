"""requirements.lock must keep up with pyproject.toml.

The image installs the lock, then llmdv itself with --no-deps, so a dependency missing from the lock
would be missing from the image and only fail at runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")

ROOT = Path(__file__).resolve().parents[1]


def _name(requirement: str) -> str:
    return re.split(r"[\s<>=!~\[;]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def test_every_runtime_dependency_is_pinned_in_the_lock():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extras = project["optional-dependencies"]
    # Both publishing backends: the Containerfile installs the lock and runs `llmdv job`, which
    # can be pointed at either auth mode, so a backend missing from the lock is missing from
    # the image. Splitting the extras changed what a `pip install` pulls, not what ships.
    required = [*project["dependencies"], *extras["s3"], *extras["ibm-cos"]]
    locked = dict(
        re.findall(
            r"^([a-z0-9][a-z0-9._-]*)==(\S+)", (ROOT / "requirements.lock").read_text(), re.M
        )
    )
    locked = {name.lower().replace("_", "-"): version for name, version in locked.items()}

    for requirement in required:
        assert _name(requirement) in locked, f"{requirement} is not in requirements.lock"
        exact = re.search(r"==\s*([^\s;]+)", requirement)
        if exact:
            assert locked[_name(requirement)] == exact.group(1), requirement
