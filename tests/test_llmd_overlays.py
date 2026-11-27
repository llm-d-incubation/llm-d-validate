"""`deploy/llm-d-nightly/overlays.yaml`: the profile llm-d's nightly runs on each lane.

The file is read by `deploy/llm-d-nightly/smoke-step.sh`, which looks a lane up by its scenario
and overlay. Each row must say what the guide README's maintainers' table says for that lane
(profile and identity flags), and every lane a README lists must be either a row or under
`not_run`, so a lane can neither run another profile than the README names nor be silently left
out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from llmdv.profile import loader

ROOT = Path(__file__).resolve().parents[1]
LLMD = ROOT / "profiles" / "llm-d"
MAP = ROOT / "deploy" / "llm-d-nightly" / "overlays.yaml"
# A scenario is a guide folder, optionally with a sub-path (`multimodal-serving/aggregation`); an
# overlay is at least two path segments (`xpu/vllm`, `gpu/vllm/lmcache-connector/cpu/gke`).
SEGMENT = r"[A-Za-z0-9._-]+"
SCENARIO = re.compile(rf"{SEGMENT}(?:/{SEGMENT})*")
OVERLAY = re.compile(rf"{SEGMENT}(?:/{SEGMENT})+")


def _map() -> dict:
    return yaml.safe_load(MAP.read_text())


def _rows() -> list[tuple[str, str, dict]]:
    return [
        (scenario, overlay, row)
        for scenario, overlays in sorted(_map()["lanes"].items())
        for overlay, row in sorted(overlays.items())
    ]


def _readme_lanes() -> dict[str, tuple[str, str | None, list[str]]]:
    """Every lane the guide READMEs' maintainers' tables list: workflow name to (guide, profile,
    identity flags). A table names its lanes by suffix after `nightly-e2e-<prefix>-`."""
    lanes = {}
    for readme in sorted(LLMD.glob("*/README.md")):
        guide = readme.parent.name
        section = readme.read_text().split("## For maintainers", 1)[-1]
        named = re.search(r"nightly-e2e-([a-z0-9-]+)-<workflow-suffix>", section)
        prefix = named.group(1) if named else guide
        for line in section.splitlines():
            if not line.startswith("| `"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            suffix = cells[0].strip("`")
            lane = suffix if suffix.startswith(prefix) else f"{prefix}-{suffix}"
            profile = re.search(r"`([a-z0-9.-]+?-standalone)(?:\.yaml)?`", line)
            identity = re.findall(r"--identity ([^ `]+)", cells[-1])
            lanes[lane] = (guide, profile.group(1) if profile else None, identity)
    return lanes


def test_every_readme_lane_is_a_row_or_not_run_and_never_both():
    readme = set(_readme_lanes())
    rows = [row["lane"] for _, _, row in _rows()]
    not_run = set(_map().get("not_run") or {})
    assert len(rows) == len(set(rows)), "a lane is listed in two rows"
    assert not set(rows) & not_run, f"listed as both a row and not_run: {set(rows) & not_run}"
    assert set(rows) | not_run == readme, (
        f"missing from overlays.yaml: {sorted(readme - set(rows) - not_run)}; "
        f"not in any guide README: {sorted((set(rows) | not_run) - readme)}"
    )


@pytest.mark.parametrize(("scenario", "overlay", "row"), _rows(), ids=lambda v: str(v)[:60])
def test_each_row_says_what_its_lanes_readme_row_says(scenario, overlay, row):
    assert SCENARIO.fullmatch(scenario), f"{scenario!r} is not a scenario path"
    assert OVERLAY.fullmatch(overlay), f"{scenario}: {overlay!r} is not an overlay path"
    assert set(row) == {"lane", "profile", "identity"}, f"{scenario}/{overlay}: {sorted(row)}"
    guide, profile, identity = _readme_lanes()[row["lane"]]
    assert scenario.split("/")[0] == guide, f"{row['lane']}: scenario {scenario} is not in {guide}/"
    assert row["profile"] == f"{guide}/{profile}", row["lane"]
    assert row["identity"] == identity, row["lane"]


@pytest.mark.parametrize("profile", sorted({row["profile"] for _, _, row in _rows()}))
def test_each_profile_exists_loads_and_reads_the_router_url(profile, monkeypatch):
    path = LLMD / f"{profile}.yaml"
    assert path.is_file(), f"no profile {path.relative_to(ROOT)}"
    monkeypatch.setenv("LLMDV_ROUTER_URL", "http://example.invalid")
    routers = [i for i in loader.load(path).ingresses if i.id == "router"]
    assert routers and routers[0].base_url_env == "LLMDV_ROUTER_URL", (
        f"{profile}: smoke-step.sh sets LLMDV_ROUTER_URL for a `router` ingress"
    )


# These are only public deployment selectors, extracted from workflow `with` inputs at the
# fixture's upstream commit. Do not copy namespace, credentials or runner-specific settings.
# Refresh alongside upstream workflow changes; unlike the README checks, this detects a map
# key typo even when the profile, lane name and identity flags are all correct.
def test_map_keys_match_upstream_workflow_inputs():
    import json

    fixture = json.loads((ROOT / "tests/fixtures/llmd_nightly_inputs.json").read_text())
    for lane, inputs in fixture["workflows"].items():
        scenario = inputs["standup_scenario"]
        overlay = "/".join(
            segment
            for key in (
                "accelerator_type",
                "backend_type",
                "connector",
                "offloading_target",
                "infra_provider",
            )
            for segment in inputs[key].split("/")
            if segment
        )
        row = _map()["lanes"].get(scenario, {}).get(overlay)
        assert row is not None, f"{lane}: upstream inputs select missing {scenario}/{overlay}"
        assert row["lane"] == lane, f"{scenario}/{overlay} selects a different workflow"
