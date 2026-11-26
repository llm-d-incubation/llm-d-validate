"""The drift report for the llm-d guide profiles, against a throwaway llm-d history."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".agents/skills/tracking-llm-d-guide-drift/drift.py"


def drift():
    spec = importlib.util.spec_from_file_location("drift", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def commit(clone: Path, message: str) -> str:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(clone), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", message)  # fmt: skip
    return run("rev-parse", "HEAD")


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """An llm-d clone with two commits, and a profiles/llm-d tree covering the first."""
    clone = tmp_path / "llm-d"
    clone.mkdir()
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    write(clone, {
        ".github/workflows/nightly-e2e-alpha-gke-gpu.yaml": "on: schedule\n",
        ".github/workflows/nightly-e2e-alpha-amd-rocm.yaml": "on: schedule\n",
        ".github/workflows/nightly-e2e-beta-gke-gpu.yaml": "on: schedule\n",
        "guides/alpha/guide.yaml": "name: alpha\n",
        "guides/alpha/modelserver/gpu/kustomization.yaml": "resources: []\n",
        "guides/nested/beta/router/beta.values.yaml": "plugins: []\n",
    })  # fmt: skip
    old = commit(clone, "pin")
    write(clone, {
        ".github/workflows/nightly-e2e-alpha-xpu.yaml": "on: schedule\n",
        ".github/workflows/nightly-e2e-gamma-gke-gpu.yaml": "on: schedule\n",
        "guides/alpha/modelserver/gpu/kustomization.yaml": "resources: [model]\n",
        "guides/nested/beta/router/beta.values.yaml": "plugins: [flow-control]\n",
        "guides/recipes/shared.yaml": "kind: Component\n",
    })  # fmt: skip
    (clone / ".github/workflows/nightly-e2e-alpha-amd-rocm.yaml").unlink()
    new = commit(clone, "upstream moved")

    profiles = tmp_path / "profiles" / "llm-d"
    write(profiles, {
        "README.md": f"Profiles at llm-d `{old[:7]}`.\n",
        "alpha/README.md": "| `gke-gpu` | ... |\n| `amd-rocm` | ... |\n",
        "beta/README.md": (
            "[guide](https://github.com/llm-d/llm-d/tree/main/guides/nested/beta)\n"
            "| `nightly-e2e-beta-gke-gpu` | ... |\n"
        ),
        "parts/README.md": "| `not-a-guide` |\n",
    })  # fmt: skip
    module = drift()
    monkeypatch.setattr(module, "GUIDES", profiles)
    return module, clone, old, new


def test_the_pin_is_read_from_the_guide_readme(stack):
    module, _, old, _ = stack
    assert module.pinned_commit() == old[:7]


def test_a_lane_no_readme_names_is_reported_under_its_guide_or_as_a_new_guide(stack):
    module, clone, old, new = stack
    text = module.report(clone, old, new)
    uncovered = text.split("## Workflows with no profile")[1].split("##")[0]
    assert "**alpha**: `nightly-e2e-alpha-xpu`" in uncovered
    assert "**(no guide folder)**: `nightly-e2e-gamma-gke-gpu`" in uncovered
    assert "alpha-gke-gpu" not in uncovered
    assert "beta" not in uncovered


def test_a_lane_gone_upstream_is_reported_removed(stack):
    module, clone, old, new = stack
    removed = module.report(clone, old, new).split("## Workflows removed")[1].split("##")[0]
    assert "`nightly-e2e-alpha-amd-rocm`" in removed


def test_changed_files_are_grouped_by_guide_and_by_the_fact_they_own(stack):
    module, clone, old, new = stack
    text = module.report(clone, old, new)
    alpha = text.split("### alpha")[1].split("###")[0]
    assert "model-server overlays" in alpha
    assert "guides/alpha/modelserver/gpu/kustomization.yaml" in alpha
    assert "workflows: which configurations get a profile" in alpha
    beta = text.split("### beta (`guides/nested/beta`)")[1].split("###")[0]
    assert "router configuration" in beta
    assert "guides/recipes/shared.yaml" in text.split("### Shared by every guide")[1]


def test_an_unchanged_guide_gets_no_section(stack):
    module, clone, old, _ = stack
    assert "###" not in module.report(clone, old, old)
