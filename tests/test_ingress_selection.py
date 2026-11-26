"""`--ingress` picks layers by id or by position, so one profile can declare a whole stack."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import yaml

from llmdv.cli.verify_cmd import select_ingresses
from llmdv.profile import loader
from tests.fakes import FakeFleet, only_run, profile_yaml, run_llmdv

ROOT = Path(__file__).resolve().parents[1]

THREE = ROOT / "profiles/examples/example-three-ingress.yaml"  # vllm (engine), router, litellm


def test_a_position_selects_every_ingress_at_it_in_profile_order():
    profile = loader.load(THREE)
    assert select_ingresses(profile, ["gateway,router"]) == (["router", "litellm"], [])
    assert select_ingresses(profile, ["gateway", "engine"]) == (["vllm", "litellm"], [])
    assert select_ingresses(profile, ["router, gateway,"]) == (["router", "litellm"], [])


def test_an_id_wins_over_a_position_and_mixes_with_one():
    profile = loader.load(THREE)
    # `router` is both an id and a position here; the id is the one ingress it names.
    assert select_ingresses(profile, ["router"]) == (["router"], [])
    assert select_ingresses(profile, ["vllm,gateway"]) == (["vllm", "litellm"], [])


def test_a_token_that_is_neither_is_returned_unplaced():
    profile = loader.load(THREE)
    assert select_ingresses(profile, ["gateway,sidecar"]) == (["litellm"], ["sidecar"])


def test_no_tokens_select_nothing_so_every_ingress_runs():
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


def test_plan_takes_positions_comma_separated(tmp_path):
    result = run_llmdv(
        "plan", "--profile", str(_three(tmp_path)), "--full-suite", "--json",
        "--ingress", "gateway,router", "-m", "discovery",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)["cases"]
    assert rows and {row["ingress"] for row in rows} == {"litellm", "router"}


def test_an_unknown_token_is_a_usage_error_naming_ids_and_positions(tmp_path):
    result = run_llmdv(
        "plan", "--profile", str(_three(tmp_path)), "--full-suite", "--ingress", "gateway,sidecar"
    )
    assert result.returncode == 2
    assert "--ingress 'sidecar'" in result.stderr
    assert "litellm (gateway)" in result.stderr and "vllm (engine)" in result.stderr


def test_verify_never_reaches_a_layer_left_out_by_position(tmp_path):
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
            "verify", "--profile", str(path), "--full-suite", "--ingress", "gateway",
            "--local-output", str(run_dir), "--pod-logs", "off",
            "-k", "test_models_lists_declared_model",
        )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    records = [
        json.loads(line) for line in (only_run(run_dir) / "results.jsonl").read_text().splitlines()
    ]
    assert records and {r["ingress"] for r in records} == {"front"}
