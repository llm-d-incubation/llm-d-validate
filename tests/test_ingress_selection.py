"""`--ingress` takes ingress ids, comma-separated or repeated, so one profile can declare a whole
stack and a run can pick its layers."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from llmdv.cli.verify_cmd import select_ingresses
from llmdv.profile import loader
from tests.fakes import FakeFleet, only_run, profile_yaml, run_llmdv

ROOT = Path(__file__).resolve().parents[1]

THREE = ROOT / "profiles/examples/example-three-ingress.yaml"  # vllm (engine), router, litellm


def test_ids_combine_comma_separated_or_repeated_in_profile_order():
    profile = loader.load(THREE)
    assert select_ingresses(profile, ["litellm,router"]) == (["router", "litellm"], [])
    assert select_ingresses(profile, ["litellm", "vllm"]) == (["vllm", "litellm"], [])
    assert select_ingresses(profile, ["router, litellm,"]) == (["router", "litellm"], [])


def test_a_repeated_id_is_selected_once():
    profile = loader.load(THREE)
    assert select_ingresses(profile, ["router,router"]) == (["router"], [])
    assert select_ingresses(profile, ["router", "router,litellm"]) == (["router", "litellm"], [])


def test_a_position_is_not_an_id():
    """Only ids select: a position names no ingress here, so it is reported, not expanded."""
    profile = loader.load(THREE)
    assert select_ingresses(profile, ["gateway"]) == ([], ["gateway"])


def test_an_unknown_id_is_returned_unplaced():
    profile = loader.load(THREE)
    assert select_ingresses(profile, ["litellm,sidecar"]) == (["litellm"], ["sidecar"])


@pytest.mark.parametrize("value", ["", " ", ",", " , ,"])
def test_a_value_that_names_no_id_is_reported_not_dropped(value):
    """An explicit value with no id in it, such as an empty shell variable, never widens the run."""
    profile = loader.load(THREE)
    assert select_ingresses(profile, [value]) == ([], [value])
    assert select_ingresses(profile, ["router", value]) == (["router"], [value])


def test_no_ids_select_nothing_so_every_ingress_runs():
    profile = loader.load(THREE)
    assert select_ingresses(profile, []) == ([], [])


def _three(tmp_path):
    raw = yaml.safe_load(THREE.read_text())
    for ingress in raw["ingresses"]:
        ingress.pop("base_url_env", None)
        ingress["base_url"] = "http://10.255.255.1:9"
        ingress["auth"] = {"type": "none"}
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def test_plan_takes_ids_comma_separated(tmp_path):
    result = run_llmdv(
        "plan", "--profile", str(_three(tmp_path)), "--full-suite", "--json",
        "--ingress", "litellm,router", "-m", "discovery",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)["cases"]
    assert rows and {row["ingress"] for row in rows} == {"litellm", "router"}


def test_an_unknown_id_is_a_usage_error_listing_the_ids(tmp_path):
    result = run_llmdv(
        "plan", "--profile", str(_three(tmp_path)), "--full-suite", "--ingress", "litellm,sidecar"
    )
    assert result.returncode == 2
    assert "--ingress 'sidecar'" in result.stderr
    assert "vllm, router, litellm" in result.stderr


@pytest.mark.parametrize("value", ["", " ", ",", " , ,"])
def test_plan_refuses_a_value_that_names_no_id(tmp_path, value):
    result = run_llmdv(
        "plan", "--profile", str(_three(tmp_path)), "--full-suite", "--ingress", value
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "names no ingress id" in result.stderr


@pytest.mark.parametrize("command", ["verify", "job"])
def test_verify_and_job_refuse_an_empty_variable_before_any_request(tmp_path, command):
    """`--ingress "$TARGET_INGRESS"` with the variable empty: exit 2, nothing sent, nothing run."""
    with FakeFleet() as fleet:
        path = tmp_path / "profile.yaml"
        path.write_text(profile_yaml(fleet.base_url, fleet.model))
        out = "--local-output" if command == "verify" else "--work-dir"
        result = run_llmdv(
            command, "--profile", str(path), "--full-suite", "--ingress", "",
            out, str(tmp_path / "run"), "--pod-logs", "off",
        )  # fmt: skip
        assert not fleet.seen
    assert result.returncode == 2, result.stdout + result.stderr
    assert "names no ingress id" in result.stdout + result.stderr


def test_verify_never_reaches_a_layer_left_out(tmp_path):
    """The unselected engine is unroutable: preflight and the cases must not touch it."""
    with FakeFleet() as fleet:
        config = yaml.safe_load(profile_yaml("http://10.255.255.1:9", fleet.model))
        engine = config["ingresses"][0]
        engine["kind"] = "engine"
        config["ingresses"].append(
            {**deepcopy(engine), "id": "front", "kind": "gateway", "base_url": fleet.base_url}
        )
        config["headers"]["x-request-id"]["front"] = "ignored"
        path = tmp_path / "profile.yaml"
        path.write_text(yaml.safe_dump(config))
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify", "--profile", str(path), "--full-suite", "--ingress", "front",
            "--local-output", str(run_dir), "--pod-logs", "off",
            "-k", "test_models_lists_declared_model",
        )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    records = [
        json.loads(line) for line in (only_run(run_dir) / "results.jsonl").read_text().splitlines()
    ]
    assert records and {r["ingress"] for r in records} == {"front"}
