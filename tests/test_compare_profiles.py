"""The profile publication comparison helper compares composed effective settings."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".agents"
    / "skills"
    / "writing-an-infra-profile"
    / "compare_profiles.py"
)


def load_comparator():
    spec = importlib.util.spec_from_file_location("compare_profiles", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROFILE = """\
schema: llmdv.profile/1
name: {name}
model:
  public_id: {model}
  max_model_len: {window}
ingresses:
  - id: engine
    kind: engine
    base_url_env: {url_env}
    auth: {{type: none}}
    surfaces: [chat_completions]
"""


def write_profiles(
    tmp_path: Path, *, local_window: int, private_window: int | None = None
) -> tuple[Path, Path]:
    public = tmp_path / "public.yaml"
    private = tmp_path / "private.yaml"
    local = tmp_path / "private.local.yaml"
    public.write_text(
        PROFILE.format(name="public", model="public/model", window=32768, url_env="PUBLIC_URL"),
        encoding="utf-8",
    )
    private.write_text(
        PROFILE.format(
            name="private",
            model="private/model",
            window=private_window if private_window is not None else local_window,
            url_env="PRIVATE_URL",
        ),
        encoding="utf-8",
    )
    local.write_text(
        "extends: public.yaml\n"
        "name: private-local\n"
        "model:\n"
        "  public_id: private/model\n"
        f"  max_model_len: {local_window}\n"
        "ingresses:\n"
        "  - id: engine\n"
        "    base_url_env: PRIVATE_URL\n",
        encoding="utf-8",
    )
    return private, local


def test_public_profile_and_local_overlay_have_matching_effective_settings(
    tmp_path, capsys, monkeypatch
):
    private, local = write_profiles(tmp_path, local_window=32768)
    comparator = load_comparator()
    monkeypatch.setattr(sys, "argv", ["compare_profiles.py", str(private), str(local), "engine"])

    comparator.main()

    assert capsys.readouterr().out.endswith("0 differ\n")


def test_comparison_names_a_changed_setting(tmp_path, capsys, monkeypatch):
    private, local = write_profiles(tmp_path, local_window=16384, private_window=32768)
    comparator = load_comparator()
    monkeypatch.setattr(sys, "argv", ["compare_profiles.py", str(private), str(local), "engine"])

    comparator.main()

    output = capsys.readouterr().out
    assert "1 differ" in output
    assert "model.max_model_len" in output


def test_comparison_rejects_an_unknown_ingress(tmp_path):
    private, _ = write_profiles(tmp_path, local_window=32768)
    comparator = load_comparator()

    with pytest.raises(SystemExit, match="no ingress 'missing'"):
        comparator.settings(private, "missing")
