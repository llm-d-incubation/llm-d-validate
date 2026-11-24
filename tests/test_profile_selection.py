"""A profile's `selection` decides which cases verify, plan and job run; `--all` sets it aside.

What the selection leaves out is deselected, never skipped, so it is in no result and no skip
class. A glob that names no case is a usage error, so a typo cannot quietly select nothing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import yaml

from llmdv.cli import job_cmd
from llmdv.cli.main import build_parser
from llmdv.profile import loader
from tests.fakes import SRC, FakeFleet, profile_yaml, run_llmdv

ROOT = SRC.parent
COMMITTED = sorted(
    [
        *(str(path.relative_to(ROOT)) for path in (ROOT / "profiles" / "examples").glob("*.yaml")),
        "deploy/overlays/example/profile.yaml",
    ]
)


def _selected(tmp_path: Path, base_url: str, *globs: str) -> Path:
    """The fake fleet's profile with `selection` set to `globs`."""
    raw = yaml.safe_load(profile_yaml(base_url))
    raw["selection"] = list(globs)
    path = tmp_path / "fake.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _plan(profile: Path, *extra: str) -> dict:
    result = run_llmdv("plan", "--profile", str(profile), "--json", *extra)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _verify(profile: Path, run_dir: Path, *extra: str):
    result = run_llmdv(
        "verify", "--profile", str(profile), "--local-output", str(run_dir),
        "--pod-logs", "off", *extra,
    )  # fmt: skip
    results = run_dir / "results.jsonl"
    records = (
        [json.loads(line) for line in results.read_text().splitlines() if line.strip()]
        if results.exists()
        else []
    )
    outcome = json.loads((run_dir / "outcome.json").read_text())
    return result, records, outcome


# --------------------------------------------------------------------- the declaration


def _load(tmp_path: Path, selection) -> loader.Profile:
    raw = yaml.safe_load(profile_yaml("http://127.0.0.1:9"))
    raw["selection"] = selection
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(raw))
    return loader.load(path)


def test_the_selection_is_read_as_globs(tmp_path):
    assert _load(tmp_path, ["discovery.*", "tools.x[y]"]).selection == ("discovery.*", "tools.x[y]")
    assert _load(tmp_path, None).selection == ()


@pytest.mark.parametrize("bad", [[], "discovery.*", [""], [3]])
def test_a_selection_that_is_not_a_list_of_globs_is_refused(tmp_path, bad):
    with pytest.raises(loader.ProfileError, match="selection"):
        _load(tmp_path, bad)


def test_a_child_clears_an_inherited_selection_with_null(tmp_path):
    parent = _selected(tmp_path, "http://127.0.0.1:9", "discovery.*")
    child = tmp_path / "child.yaml"
    child.write_text(f"extends: {parent.name}\nselection: null\n")
    assert loader.load(child).selection == ()


# --------------------------------------------------------------------- plan


def test_plan_lists_only_the_selected_cases_and_counts_the_rest(tmp_path):
    profile = _selected(tmp_path, "http://127.0.0.1:9", "discovery.*")
    selected = _plan(profile)
    everything = _plan(profile, "--all")

    assert selected["cases"]
    assert {row["case_id"].split(".")[0] for row in selected["cases"]} == {"discovery"}
    left_out = selected["selection"]["profile"]
    assert left_out == {
        "cases": ["discovery.*"],
        "left_out": everything["counts"]["total"] - selected["counts"]["total"],
    }
    assert left_out["left_out"] > 0
    # The cases it left out are in no skip class: the counts are of what was selected.
    assert sum(selected["counts"]["by_skip_class"].values()) == selected["counts"]["skip"]
    assert everything["selection"]["all"] is True
    assert everything["selection"]["profile"]["left_out"] == 0
    assert {row["case_id"].split(".")[0] for row in everything["cases"]} > {"discovery"}


def test_plan_text_says_how_many_were_left_out_and_how_to_run_them(tmp_path):
    profile = _selected(tmp_path, "http://127.0.0.1:9", "discovery.*")
    result = run_llmdv("plan", "--profile", str(profile), "-q")
    assert result.returncode == 0, result.stderr
    left_out = _plan(profile)["selection"]["profile"]["left_out"]
    assert f"{left_out} left out by the profile's selection (--all runs them)" in result.stdout

    full = run_llmdv("plan", "--profile", str(profile), "-q", "--all")
    assert "left out" not in full.stdout


def test_other_selectors_narrow_within_the_selection(tmp_path):
    profile = _selected(tmp_path, "http://127.0.0.1:9", "discovery.*", "contract.*")
    by_surface = _plan(profile, "--surface", "messages")
    by_keyword = _plan(profile, "-k", "model_info")

    surfaces = {row["surface"] for row in by_surface["cases"]}
    assert surfaces <= {"messages", None} and "messages" in surfaces
    suites = {row["case_id"].split(".")[0] for row in by_surface["cases"]}
    assert suites <= {"discovery", "contract"}
    assert {row["case_id"] for row in by_keyword["cases"]} == {"discovery.model_info"}


def test_a_variant_glob_selects_that_variant_alone(tmp_path):
    case = "discovery.unknown_model_no_fallback"
    profile = _selected(tmp_path, "http://127.0.0.1:9", f"{case}[messages]")
    rows = _plan(profile)["cases"]
    assert [(row["case_id"], row["variant"]) for row in rows] == [(case, "messages")]


def test_a_glob_matching_nothing_is_a_usage_error_from_plan(tmp_path):
    profile = _selected(tmp_path, "http://127.0.0.1:9", "discovery.*", "discovry.*")
    result = run_llmdv("plan", "--profile", str(profile))
    assert result.returncode == 2, result.stderr
    assert "'discovry.*'" in result.stderr
    assert "'discovery.*'" not in result.stderr
    # --all sets the selection aside, bad glob included.
    assert run_llmdv("plan", "--profile", str(profile), "-q", "--all").returncode == 0


@pytest.mark.parametrize("example", COMMITTED)
def test_every_committed_profile_plans_every_case(example):
    """No committed profile declares a selection, so each plans exactly what --all plans."""
    path = ROOT / example
    assert loader.load(path).selection == ()
    plain, everything = _plan(path), _plan(path, "--all")
    assert plain["selection"]["profile"] == {"cases": [], "left_out": 0}
    assert [row["nodeid"] for row in plain["cases"]] == [
        row["nodeid"] for row in everything["cases"]
    ]
    assert plain["counts"] == everything["counts"]


# --------------------------------------------------------------------- verify and job


def test_verify_runs_only_the_selection_and_records_it(tmp_path):
    with FakeFleet() as fleet:
        profile = _selected(tmp_path, fleet.base_url, "discovery.*")
        result, records, outcome = _verify(profile, tmp_path / "run")

    assert result.returncode == 0, result.stderr[-3000:]
    assert records
    assert {record["case_id"].split(".")[0] for record in records} == {"discovery"}
    assert sum(outcome["verification"]["counts"].values()) == len(records)
    assert sum(outcome["verification"]["skips"].values()) == sum(
        1 for record in records if record["outcome"] == "skip"
    )
    assert outcome["selection"] == {"cases": ["discovery.*"], "all": False, "in_force": True}
    assert "case(s) left out by the profile's selection" in result.stderr


def test_verify_all_sets_the_selection_aside(tmp_path):
    with FakeFleet() as fleet:
        profile = _selected(tmp_path, fleet.base_url, "discovery.*")
        result, records, outcome = _verify(
            profile, tmp_path / "run", "--all", "-k", "models_lists or declared_surfaces_exist"
        )

    assert result.returncode == 0, result.stderr[-3000:]
    assert {record["case_id"] for record in records} == {
        "discovery.models_lists_declared_model",
        "contract.declared_surfaces_exist",
    }
    assert outcome["selection"] == {"cases": ["discovery.*"], "all": True, "in_force": False}


def test_a_glob_matching_nothing_is_a_usage_error_from_verify(tmp_path):
    with FakeFleet() as fleet:
        profile = _selected(tmp_path, fleet.base_url, "discovry.*")
        result, records, outcome = _verify(profile, tmp_path / "run")

    assert result.returncode == 2, result.stderr[-3000:]
    assert "'discovry.*'" in result.stderr
    assert records == []
    assert outcome["verification"]["class"] == "usage"


def test_a_glob_matching_nothing_is_refused_before_preflight(tmp_path):
    """Nothing listens on the port, so had preflight run first this would be exit 3 (the
    operator's environment) and never name the typo."""
    profile = _selected(tmp_path, "http://127.0.0.1:9", "discovry.*")
    result, records, _ = _verify(profile, tmp_path / "run")
    assert result.returncode == 2, result.stderr[-3000:]
    assert "selection: 'discovry.*' matches no case" in result.stderr
    assert "preflight" not in result.stderr
    assert records == []


def test_verify_without_a_selection_records_none(tmp_path):
    with FakeFleet() as fleet:
        profile = tmp_path / "fake.yaml"
        profile.write_text(profile_yaml(fleet.base_url))
        result, _, outcome = _verify(profile, tmp_path / "run", "-k", "models_lists")

    assert result.returncode == 0, result.stderr[-3000:]
    assert outcome["selection"] == {"cases": [], "all": False, "in_force": False}


def test_job_passes_all_through_to_verify(tmp_path, monkeypatch):
    args, unknown = build_parser().parse_known_args(["job", "--profile", "p.yaml", "--all"])
    assert unknown == ["--all"]

    started: list[list[str]] = []

    class Child:
        def __init__(self, command):
            started.append(command)

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(job_cmd.subprocess, "Popen", Child)
    monkeypatch.setattr(job_cmd.signal, "signal", lambda *a: None)
    namespace = argparse.Namespace(**{**vars(args), "remote_output": None})
    assert job_cmd._verify(namespace, tmp_path, unknown) == 0
    (command,) = started
    assert command[command.index("verify") :].count("--all") == 1


def test_a_selection_naming_the_renamed_case_names_its_replacement(tmp_path):
    """As a known_failures entry does: otherwise it fails later as a glob matching nothing."""
    profile = _selected(tmp_path, "http://127.0.0.1:9", "field_options.vllm_observable")
    with pytest.raises(loader.ProfileError, match="use 'field_options.extension_observable'"):
        loader.load(profile)


GUIDE_COMBINATIONS = sorted(
    str(path.relative_to(ROOT))
    for path in (ROOT / "profiles" / "llm-d").glob("*/*.yaml")
    if path.parent.name != "parts"
)


@pytest.mark.parametrize("combination", GUIDE_COMBINATIONS)
def test_every_guide_combination_plans_under_its_selection(combination):
    """Every glob a guide selects matches a case (a renamed or removed case fails here, offline,
    rather than as exit 2 on a lane), and the selection keeps the contract cases."""
    path = ROOT / combination
    assert loader.load(path).selection, "a guide combination declares a selection"
    plan = _plan(path)
    assert plan["selection"]["profile"]["left_out"] > 0
    assert any(row["case_id"].startswith("contract.") for row in plan["cases"])
