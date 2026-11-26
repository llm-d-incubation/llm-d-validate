"""Coverage documentation follows composed profiles and their actual source parts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from llmdv.profile import loader


@pytest.fixture
def generator():
    path = (
        Path(__file__).resolve().parents[1]
        / ".agents/skills/writing-an-infra-profile/selection_docs.py"
    )
    spec = importlib.util.spec_from_file_location("selection_docs", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_identical_defaults_from_different_parts_name_both_sources(generator, tmp_path):
    entries = {}
    for name in ("alpha", "beta"):
        part = tmp_path / f"{name}-part.yaml"
        part.write_text('test_sets: {default: ["*"]}\n')
        profile = tmp_path / f"{name}.yaml"
        profile.write_text(f"extends: {part.name}\n")
        sets, sources = generator.composed_sets(profile)
        plan = {"total": 4, "run": 3, "skip": 1, "suites": ["tools"], "skips": {"undeclared": 1}}
        entries[profile] = {
            "sets": sets,
            "sources": sources,
            "plans": {scope: plan for scope in generator.SCOPES},
        }
    text = generator.section(tmp_path, entries)
    assert "alpha-part.yaml" in text and "beta-part.yaml" in text
    assert "All suites" in text
    assert "3" in text and "undeclared" in text


@pytest.mark.parametrize("duplicate", [False, True])
def test_invalid_inheritance_uses_the_profile_loader_error(generator, tmp_path, duplicate):
    profile = tmp_path / "profile.yaml"
    part = tmp_path / "part.yaml"
    if duplicate:
        part.write_text('test_sets: {default: ["*"]}\n')
        profile.write_text("extends: [part.yaml, part.yaml]\n")
    else:
        profile.write_text("extends: part.yaml\n")
        part.write_text("extends: profile.yaml\n")
    with pytest.raises(loader.ProfileError):
        generator.composed_sets(profile)


def test_shared_part_is_the_source_and_parts_are_not_runnable(generator, tmp_path, monkeypatch):
    parts = tmp_path / "parts"
    parts.mkdir()
    base = parts / "base.yaml"
    base.write_text('test_sets: {default: ["*"]}\n')
    profile = tmp_path / "model.yaml"
    profile.write_text("extends: parts/base.yaml\n")
    (tmp_path / "private.local.yaml").write_text("extends: model.yaml\n")
    monkeypatch.setattr(generator, "PROFILES", tmp_path)
    assert generator.runnable() == [profile]
    sets, sources = generator.composed_sets(profile)
    assert sets == {"default": ["*"]}
    assert sources == {"default": base.resolve()}
