"""`model.public_id_env` lets one profile test a layer that serves the model under its own name."""

from __future__ import annotations

import json

import pytest
import yaml

from llmdv.profile import loader
from tests.fakes import FakeFleet, profile_yaml, run_llmdv

GATEWAY_MODEL = "LLMDV_TEST_GATEWAY_MODEL"
ROUTER_MODEL = "LLMDV_TEST_ROUTER_MODEL"


def _profile(tmp_path, model=None, gateway_model=None, router_model=None):
    raw = {
        "schema": "llmdv.profile/1",
        "name": "model-id-env",
        "model": {"public_id": "org/public-model", **(model or {})},
        "ingresses": [
            {"id": "litellm", "kind": "gateway", "base_url": "http://10.255.255.1:9",
             **({"model": gateway_model} if gateway_model is not None else {})},
            {"id": "router", "kind": "router", "base_url": "http://10.255.255.1:9",
             **({"model": router_model} if router_model is not None else {})},
        ],
        "test_sets": {"default": ["contract.*"]},
    }  # fmt: skip
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _ids(path):
    return {ingress.id: ingress.model.public_id for ingress in loader.load(path).ingresses}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(GATEWAY_MODEL, raising=False)
    monkeypatch.delenv(ROUTER_MODEL, raising=False)


def test_public_id_is_the_default_while_the_variable_is_unset(tmp_path):
    path = _profile(tmp_path, gateway_model={"public_id_env": GATEWAY_MODEL})
    assert _ids(path) == {"litellm": "org/public-model", "router": "org/public-model"}


def test_the_variable_supplies_the_id_on_the_ingress_that_declares_it(tmp_path, monkeypatch):
    monkeypatch.setenv(GATEWAY_MODEL, "alias/public-model-staging")
    path = _profile(tmp_path, gateway_model={"public_id_env": GATEWAY_MODEL})
    assert _ids(path) == {"litellm": "alias/public-model-staging", "router": "org/public-model"}


def test_each_layer_can_name_its_own_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(GATEWAY_MODEL, "alias/a")
    monkeypatch.setenv(ROUTER_MODEL, "alias/b")
    path = _profile(
        tmp_path,
        gateway_model={"public_id_env": GATEWAY_MODEL},
        router_model={"public_id_env": ROUTER_MODEL},
    )
    assert _ids(path) == {"litellm": "alias/a", "router": "alias/b"}


def test_a_profile_level_variable_is_inherited_and_null_stops_it(tmp_path, monkeypatch):
    monkeypatch.setenv(GATEWAY_MODEL, "alias/a")
    path = _profile(
        tmp_path, model={"public_id_env": GATEWAY_MODEL}, router_model={"public_id_env": None}
    )
    assert _ids(path) == {"litellm": "alias/a", "router": "org/public-model"}


def test_an_empty_variable_falls_back_to_public_id(tmp_path, monkeypatch):
    monkeypatch.setenv(GATEWAY_MODEL, "  ")
    path = _profile(tmp_path, gateway_model={"public_id_env": GATEWAY_MODEL})
    assert _ids(path)["litellm"] == "org/public-model"


def test_a_run_requests_and_checks_the_id_the_variable_names(tmp_path):
    """Without the variable, preflight refuses the id the endpoint does not serve; with it, the
    run asks for and expects the served name."""
    with FakeFleet(model="alias/served-name") as fleet:
        config = yaml.safe_load(profile_yaml(fleet.base_url, "org/public-model"))
        config["model"]["public_id_env"] = GATEWAY_MODEL
        path = tmp_path / "profile.yaml"
        path.write_text(yaml.safe_dump(config))
        run_dir = tmp_path / "run"

        def verify(**env):
            return run_llmdv(
                "verify", "--profile", str(path), "--full-suite", "--local-output", str(run_dir),
                "--pod-logs", "off", "-k", "test_models_lists_declared_model", env=env,
            )  # fmt: skip

        without = verify()
        with_name = verify(**{GATEWAY_MODEL: "alias/served-name"})
    assert without.returncode == 3, without.stdout + without.stderr
    assert "'org/public-model'" in without.stderr
    assert with_name.returncode == 0, with_name.stdout + with_name.stderr
    newest = sorted(run_dir.glob("*/results.jsonl"))[-1]
    records = [json.loads(line) for line in newest.read_text().splitlines()]
    assert records and all(r["outcome"] == "pass" for r in records)
