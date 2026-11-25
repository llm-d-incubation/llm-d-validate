"""Authoring views preserve contracts and explain overrides without resolving secrets."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from llmdv.profile import inspection, loader
from tests.fakes import run_llmdv
from tests.test_profile_extends import PARENT, child_of, write


def rows(path):
    profile, chain = loader.load_with_sources(path)
    return {row.path: row for row in inspection.explain(profile, chain)}


def test_ingress_overrides_beat_later_global_defaults(tmp_path):
    parent = deepcopy(PARENT)
    parent["ingresses"][0]["model"] = {"max_model_len": 1000}
    parent["ingresses"][0]["capabilities"] = {"count_tokens": False}
    path = child_of(tmp_path, {"model": {"max_model_len": 2000}}, parent)
    result = rows(path)
    gateway = result["ingresses[gateway].model.max_model_len"]
    assert gateway.value == 1000
    assert [a.value for a in gateway.assignments] == [2000, 1000]
    assert [a.source.name for a in gateway.assignments] == ["local.yaml", "shared.yaml"]
    assert result["ingresses[engine].model.max_model_len"].value == 2000
    assert result["ingresses[gateway].capabilities.count_tokens"].value is False


def test_replacements_resets_empty_mappings_and_same_value_assignments(tmp_path):
    path = child_of(
        tmp_path,
        {
            "model": {"reasoning": {}, "public_id": "org/model"},
            "capabilities": {"extension_options": [], "count_tokens": None},
        },
    )
    result = rows(path)
    assert result["model.reasoning.parser"].value == "deepseek_r1"
    assert len(result["model.reasoning.parser"].assignments) == 1
    assert result["capabilities.extension_options"].value == []
    assert [a.value for a in result["capabilities.extension_options"].assignments] == [
        ["priority"],
        [],
    ]
    assert result["capabilities.count_tokens"].value is False
    assert result["capabilities.count_tokens"].assignments[-1].value is None
    assert len(result["model.public_id"].assignments) == 2
    assert result["name"].value == "local"
    assert result["name"].assignments[0].path == "(file name)"


def test_multiple_parents_and_cleared_ancestor(tmp_path):
    write(tmp_path / "base.yaml", PARENT)
    write(tmp_path / "override.yaml", {"model": {"reasoning": None}})
    path = write(tmp_path / "leaf.yaml", {"extends": ["base.yaml", "override.yaml"]})
    row = rows(path)["model.reasoning.parser"]
    assert row.value is None
    assert row.assignments[-1].path == "model.reasoning"
    assert row.assignments[-1].source.name == "override.yaml"


def test_field_queries_include_defaults_and_reject_unknown_fields(tmp_path):
    profile, chain = loader.load_with_sources(child_of(tmp_path, {}))
    text = inspection.explain_text(profile, chain, "model.tools")
    assert "model.tools.parser = null" in text
    assert "from llmdv default" in text
    with pytest.raises(loader.ProfileError, match="no profile field"):
        inspection.explain_text(profile, chain, "model.typo")


@pytest.mark.parametrize("composed", [False, True])
def test_export_reloads_elsewhere_and_never_embeds_environment(tmp_path, monkeypatch, composed):
    parent = deepcopy(PARENT)
    parent.pop("name")
    ingress = parent["ingresses"][0]
    ingress.pop("base_url")
    ingress["base_url_env"] = "TEST_PROFILE_URL"
    ingress["auth"] = {"type": "bearer", "key_env": "TEST_PROFILE_KEY"}
    parent["clients"] = [
        {
            "id": "cc",
            "surfaces": ["chat_completions"],
            "binaries": [{"version": "2.1.0", "executable": "./claude"}],
        }
    ]
    path = child_of(tmp_path, {}, parent) if composed else write(tmp_path / "original.yaml", parent)
    monkeypatch.setenv("TEST_PROFILE_URL", "https://private.example/secret-url")
    monkeypatch.setenv("TEST_PROFILE_KEY", "secret-api-key")
    profile, chain = loader.load_with_sources(path)
    exported = inspection.export_yaml(chain)
    explained = inspection.explain_text(profile, chain)
    for text in (exported, explained):
        assert "secret-url" not in text
        assert "secret-api-key" not in text
        assert "TEST_PROFILE_URL" in text
        assert "TEST_PROFILE_KEY" in text
    document = yaml.safe_load(exported)
    assert "extends" not in document
    target = tmp_path / "elsewhere" / "renamed.yaml"
    write(target, document)
    reloaded = loader.load(target)
    assert reloaded.name == profile.name
    assert reloaded.model == profile.model
    assert reloaded.ingresses == profile.ingresses
    assert reloaded.clients == profile.clients
    assert reloaded.clients[0].binaries[0].executable == tmp_path / "claude"


def test_cli_export_explain_and_usage_errors(tmp_path):
    # The real CLI in a subprocess also checks primary output remains valid YAML.
    path = child_of(tmp_path, {})
    exported = run_llmdv("profile", "show", str(path), "--yaml")
    assert exported.returncode == 0, exported.stderr
    assert yaml.safe_load(exported.stdout)["name"] == "local"
    explained = run_llmdv("profile", "explain", str(path), "--field", "model.public_id")
    assert explained.returncode == 0, explained.stderr
    assert "shared.yaml" in explained.stdout
    assert run_llmdv("profile", "explain", str(path), "--field", "typo").returncode == 2
    assert run_llmdv("profile", "show", str(path), "--yaml", "--setup").returncode == 2
    invalid = write(tmp_path / "invalid.yaml", {"schema": "llmdv.profile/1", "typo": True})
    assert run_llmdv("profile", "explain", str(invalid)).returncode == 2
    assert run_llmdv("profile", "show", str(invalid), "--yaml").returncode == 2


def test_all_complete_examples_can_be_explained_and_exported(tmp_path):
    for path in Path("profiles").rglob("*.yaml"):
        if "parts" in path.parts or path.name.endswith(".local.yaml"):
            continue
        profile, chain = loader.load_with_sources(path)
        assert inspection.explain_text(profile, chain)
        destination = write(tmp_path / "export.yaml", yaml.safe_load(inspection.export_yaml(chain)))
        reloaded = loader.load(destination)
        assert reloaded == replace(profile, source_path=destination)


def test_selection_and_clients_show_effective_values_after_clearing(tmp_path):
    parent = {**PARENT, "selection": ["contract.*"], "clients": [{"id": "raw"}]}
    path = child_of(tmp_path, {"selection": None, "clients": None}, parent)
    result = rows(path)
    assert result["selection"].value == []
    assert result["selection"].assignments[-1].value is None
    assert result["clients"].value == []


def test_ingress_lists_merge_but_surface_lists_replace(tmp_path):
    path = child_of(tmp_path, {"ingresses": [{"id": "gateway", "surfaces": []}]})
    result = rows(path)
    assert result["ingresses[gateway].surfaces"].value == []
    assert [a.value for a in result["ingresses[gateway].surfaces"].assignments] == [
        ["chat_completions", "messages"],
        [],
    ]
    path = child_of(tmp_path, {"ingresses": []})
    assert rows(path)["ingresses[gateway].surfaces"].value == ["chat_completions", "messages"]
