"""`profiles/llm-d/.overlays.yaml`: the profile llm-d's nightly runs for each overlay.

The file is data for llm-d's CI, kept beside the profiles it names. Each guide it lists must say
exactly what that guide's README says under "Pick your profile", in both directions, so a row can
never point at another profile than a reader would choose, and an overlay the README covers is
never silently left out. An overlay that is not listed has no profile; the nightly skips it.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

from llmdv.profile import loader

ROOT = Path(__file__).resolve().parents[1]
LLMD = ROOT / "profiles" / "llm-d"
OVERLAYS = LLMD / ".overlays.yaml"
# An overlay is a path under guides/<guide>/modelserver/: segments with no empty one, no slash at
# either end and no whitespace (`gpu/vllm/gke`, `tpu/v5p/vllm`, `xpu/vllm`).
OVERLAY = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+")


def _overlays() -> dict[str, dict[str, str]]:
    return yaml.safe_load(OVERLAYS.read_text()) or {}


def _readme_map(guide: str) -> dict[str, str]:
    """Overlay to profile, read from the guide README's "Pick your profile" table."""
    text = (LLMD / guide / "README.md").read_text()
    table = text.split("## Pick your profile", 1)[1].split("\n## ", 1)[0]
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in table.splitlines()
        if line.startswith("|")
    ]
    header, body = rows[0], rows[2:]
    assert header[1] in ("Overlay", "Overlays"), (
        f"{guide}: 'Pick your profile' column 2 is {header[1]!r}, not the overlays a lane deploys; "
        "a guide whose profiles are not chosen by overlay cannot be listed in .overlays.yaml"
    )
    found: dict[str, str] = {}
    for cells in body:
        link = re.match(r"\[([^\]]+)\]\(([^)]+)\)", cells[2])
        assert link, f"{guide}: a 'Pick your profile' row has no profile link: {cells}"
        for overlay in re.findall(r"`([^`]+)`", cells[1]):
            assert overlay not in found, f"{guide}: overlay {overlay} is in two rows"
            found[overlay] = link.group(1)
    return found


def test_the_file_is_a_mapping_of_guides_to_overlays_to_profile_names():
    data = _overlays()
    assert data, ".overlays.yaml lists no guide"
    for guide, rows in data.items():
        assert (LLMD / guide).is_dir(), f"{guide}: no such guide folder under profiles/llm-d/"
        assert isinstance(rows, dict) and rows, f"{guide}: expected overlay: profile rows"
        for overlay, profile in rows.items():
            assert OVERLAY.fullmatch(overlay), f"{guide}: {overlay!r} is not an overlay path"
            assert isinstance(profile, str) and not profile.endswith(".yaml"), (
                f"{guide}/{overlay}: name the profile file without .yaml, got {profile!r}"
            )


@pytest.mark.parametrize("guide", sorted(_overlays()))
def test_each_guide_matches_its_readme_pick_your_profile_table(guide):
    assert _overlays()[guide] == _readme_map(guide)


@pytest.mark.parametrize(
    ("guide", "overlay", "profile"),
    [(g, o, p) for g, rows in sorted(_overlays().items()) for o, p in rows.items()],
)
def test_each_listed_profile_exists_and_loads(guide, overlay, profile, monkeypatch):
    path = LLMD / guide / f"{profile}.yaml"
    assert path.is_file(), f"{guide}/{overlay}: no profile {path.relative_to(ROOT)}"
    for name in ("LLMDV_ROUTER_URL", "LLMDV_GATEWAY_URL"):
        monkeypatch.setenv(name, "http://example.invalid")
    loader.load(path)


def test_the_overlay_file_is_never_taken_for_a_profile():
    """Every walk over profiles/**/*.yaml skips dotfiles: the suite's and the docs generator's."""
    from tests import test_example_profiles

    assert OVERLAYS not in test_example_profiles.FILES
    script = ROOT / ".agents/skills/writing-an-infra-profile/selection_docs.py"
    spec = importlib.util.spec_from_file_location("selection_docs", script)
    selection_docs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selection_docs)
    assert OVERLAYS not in selection_docs.runnable()
