"""Every committed profile loads and validates: the examples, and the real profiles beside them.

Nothing checked this before. A committed example that does not load is a trap for the first person
who copies it, and they will reasonably assume the error is theirs. The examples are also the only
documentation of the schema that is executable, so a schema change that breaks one should fail here
rather than in somebody's terminal.

Validation is offline by design -- no cluster, no credentials -- which is exactly what makes it
safe to assert in a unit test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from llmdv.profile import loader

PROFILES = Path(__file__).resolve().parents[1] / "profiles"
EXAMPLES = sorted((PROFILES / "examples").glob("*.yaml"))
# Every committed profile, at any depth. Local variants are gitignored and never checked here.
COMMITTED = sorted(p for p in PROFILES.rglob("*.yaml") if not p.name.endswith(".local.yaml"))
REAL = [p for p in COMMITTED if p not in EXAMPLES]


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
