"""Every committed profile loads and validates: the examples, and the real profiles beside them.

Nothing checked this before. A committed example that does not load is a trap for the first person
who copies it, and they will reasonably assume the error is theirs. The examples are also the only
documentation of the schema that is executable, so a schema change that breaks one should fail here
rather than in somebody's terminal.

Validation is offline by design -- no cluster, no credentials -- which is exactly what makes it
safe to assert in a unit test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from llmdv.profile import compose, loader

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"
EXAMPLES = sorted((PROFILES / "examples").glob("*.yaml"))
# Every committed file, at any depth. Local variants are gitignored and never checked here.
FILES = sorted(p for p in PROFILES.rglob("*.yaml") if not p.name.endswith(".local.yaml"))
# A part is a file another file extends that is not a profile on its own: it lacks a model or an
# ingress (a model part has no ingress, a topology part no model). It is checked through every
# profile that extends it, never alone. A complete parent, such as an example a staging variant
# extends, is still a profile and is checked alone too.
PARTS = sorted(
    {
        layer.path.resolve()
        for path in FILES
        for layer in compose.layers(path)[:-1]
        if not ("model" in layer.raw and "ingresses" in layer.raw)
    }
)
COMMITTED = [p for p in FILES if p.resolve() not in PARTS]
REAL = [p for p in COMMITTED if p not in EXAMPLES]


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(PROFILES)))
def test_a_committed_file_carries_no_credential_value(path: Path):
    """Parts included: a literal key must never be committed anywhere under profiles/."""
    text = path.read_text(encoding="utf-8")
    for marker in ("sk-", "Bearer ", "api_key:", "apikey:"):
        assert marker not in text, f"{path.name} contains {marker!r}, which looks like a secret"


def test_there_are_examples_to_check():
    """Guards the glob: a rename that empties it would otherwise make every test below vacuous."""
    assert EXAMPLES, f"no examples/*.yaml found under {PROFILES}"


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: str(p.relative_to(PROFILES)))
def test_an_example_profile_loads(path: Path):
    profile = loader.load(path)
    assert profile.name, f"{path.name} loaded with no name"
    assert profile.ingresses, f"{path.name} declares no ingresses"


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: str(p.relative_to(PROFILES)))
def test_an_example_profile_validates_without_errors(path: Path):
    """Warnings are fine: a surface declared that this build cannot drive is one. Errors are not."""
    errors = [item for item in loader.validate(loader.load(path)) if item.level == "error"]
    assert not errors, f"{path.name}: " + "; ".join(item.message for item in errors)


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: str(p.relative_to(PROFILES)))
def test_an_example_profile_carries_no_credential_value(path: Path):
    """Examples name credentials by environment variable. A literal key must never be committed."""
    text = path.read_text(encoding="utf-8")
    for marker in ("sk-", "Bearer ", "api_key:", "apikey:"):
        assert marker not in text, f"{path.name} contains {marker!r}, which looks like a secret"


@pytest.mark.parametrize("path", REAL, ids=lambda p: str(p.relative_to(PROFILES)))
def test_a_real_profile_names_its_url_by_variable(path: Path):
    """A real hostname is cluster-identifiable, so a committed real profile uses base_url_env."""
    for ingress in loader.load(path).ingresses:
        assert ingress.base_url_env, f"{path.name}: ingress {ingress.id} hardcodes base_url"


def _image_claude_code_version() -> str:
    text = (ROOT / "Containerfile").read_text()
    return re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", text, re.M).group(1)


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(PROFILES)))
def test_a_client_at_the_images_path_pins_the_images_version(path):
    """The runner image installs one Claude Code at /usr/local/bin/claude. A profile that pins
    another version there fails every client case in that image before it checks anything, so
    it must name the image's version, or point at its own install path."""
    raw = yaml.safe_load(path.read_text()) or {}
    for client in raw.get("clients") or []:
        for binary in client.get("binaries") or []:
            if binary.get("executable") == "/usr/local/bin/claude":
                assert str(binary.get("version")) == _image_claude_code_version(), (
                    f"{client.get('id')}: pins {binary.get('version')} at /usr/local/bin/claude, "
                    f"where the image installs {_image_claude_code_version()}"
                )
