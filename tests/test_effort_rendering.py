"""The rendered-effort cases read the effort the model was given out of its prompt, and fail when
the template renders another one, renders none, or the prompt cannot be read back."""

from __future__ import annotations

import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from tests.fakes import RENDERED_EFFORTS, FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile

PATTERN = r"Reasoning Effort: (\w+)"
ALL_SURFACES = ["chat_completions", "responses", "messages"]


def declare(path, efforts=None, surfaces=None, **extra):
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"]["effort_rendering"] = {
        "pattern": PATTERN,
        "efforts": dict(RENDERED_EFFORTS) if efforts is None else efforts,
        "surfaces": surfaces or ALL_SURFACES,
        **extra,
    }
    path.write_text(yaml.safe_dump(raw))


def run(tmp_path, efforts=RENDERED_EFFORTS, defect=None, declared=None, surfaces=None):
    with FakeFleet(effort_render=dict(efforts), effort_render_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        declare(path, declared, surfaces)
        return run_suites(path, tmp_path / "run", "-k", "effort_rendered")


def by_variant(records):
    return {r["nodeid"].split("::")[-1]: r for r in records}


def check(record, name):
    return next(c for c in record["checks"] if c["name"] == name)


def test_every_declared_effort_passes_on_a_faithful_template(tmp_path):
    code, records = run(tmp_path)
    assert code == 0, records
    ran = [r for r in records if r["outcome"] == "pass"]
    # Chat and Responses carry unset and seven efforts each; Messages unset and five.
    assert len(ran) == 8 + 8 + 6, [r["nodeid"] for r in records]
    for record in ran:
        assert check(record, "rendered_effort")["ok"]
        assert check(record, "prompt_logprobs")["ok"]


def test_one_effort_rendered_at_the_wrong_level_fails(tmp_path):
    code, records = run(tmp_path, defect="wrong_level:medium")
    assert code == 1
    failed = [r for r in records if r["outcome"] == "fail"]
    # medium exists on all three APIs, and only medium is wrong.
    assert len(failed) == 3, [r["nodeid"] for r in failed]
    for record in failed:
        assert "medium" in record["nodeid"]
        rendered = check(record, "rendered_effort")
        assert rendered["ok"] is False
        assert "High" in str(rendered["observed"])


def test_a_template_that_renders_no_marker_fails(tmp_path):
    code, records = run(tmp_path, defect="no_marker", surfaces=["chat_completions"])
    assert code == 1
    failed = [r for r in records if r["outcome"] == "fail"]
    assert len(failed) == 8 and len(records) == 8 + 8 + 6
    assert all(check(r, "rendered_effort")["observed"].startswith("{'markers': []") for r in failed)


def test_missing_prompt_logprobs_fails_on_a_declared_surface(tmp_path):
    code, records = run(tmp_path, defect="drops_prompt_logprobs")
    assert code == 1
    assert {r["outcome"] for r in records} == {"fail"}
    for record in records:
        logprobs = check(record, "prompt_logprobs")
        assert logprobs["ok"] is False
        assert logprobs["observed"] == "absent"


def test_an_effort_declared_to_render_no_marker(tmp_path):
    """A template that drops its effort line for `none`, declared as null, passes; one that
    still renders a marker fails."""
    efforts = {"none": None, "high": "High"}
    code, records = run(tmp_path, efforts=efforts, declared=efforts, surfaces=["chat_completions"])
    assert code == 0, records
    assert sorted(r["outcome"] for r in records).count("pass") == 2

    declared = {"none": None}
    code, records = run(
        tmp_path / "marked", efforts={"none": "Low"}, declared=declared, surfaces=["responses"]
    )
    assert code == 1
    failed = [r for r in records if r["outcome"] == "fail"]
    assert len(failed) == 1 and "none" in failed[0]["nodeid"]
    assert "'markers': ['Low']" in str(check(failed[0], "rendered_effort")["observed"])


def test_undeclared_surfaces_and_efforts_skip_as_undeclared(tmp_path):
    code, records = run(tmp_path, declared={"high": "High"}, surfaces=["chat_completions"])
    assert code == 0, records
    variants = by_variant(records)
    passed = [name for name, r in variants.items() if r["outcome"] == "pass"]
    assert len(passed) == 1 and "high" in passed[0]
    skipped = [r for r in records if r["outcome"] == "skip"]
    assert len(skipped) == len(records) - 1
    assert {r.get("skip_class") for r in skipped} == {"undeclared"}


BASE = """
schema: llmdv.profile/1
name: p
vantage: external
model: {public_id: m}
capabilities:
  effort_rendering: %s
ingresses:
  - id: a
    kind: vllm
    base_url: http://localhost:1
    auth: {type: none}
    surfaces: [chat_completions]
"""


def load_with(tmp_path, block, ingress_extra=""):
    path = tmp_path / "p.yaml"
    path.write_text(BASE % block + ingress_extra)
    return load(path)


def test_profile_parses_markers_and_defaults(tmp_path):
    profile = load_with(
        tmp_path, "{pattern: 'Effort: (\\d+)', efforts: {unset: 75, none: null, low: '50'}}"
    )
    config = profile.capabilities.effort_rendering
    assert config.efforts == {"unset": "75", "none": None, "low": "50"}
    assert config.surfaces == ("chat_completions",)


@pytest.mark.parametrize(
    "block,message",
    [
        ("{pattern: 'Effort: \\w+', efforts: {low: Low}}", "exactly one capture group"),
        ("{pattern: '(', efforts: {low: Low}}", "not a regular expression"),
        ("{pattern: '(\\w+)', efforts: {lowest: Low}}", "unknown effort"),
        ("{pattern: '(\\w+)', efforts: {low: ''}}", "the marker text"),
        ("{pattern: '(\\w+)', efforts: {}}", "expected a mapping of effort"),
        ("{pattern: '(\\w+)', efforts: {low: Low}, surfaces: [completions]}", "surfaces"),
        ("{pattern: '(\\w+)', efforts: {low: Low}, marker: x}", "unknown field"),
    ],
)
def test_profile_refuses_an_invalid_declaration(tmp_path, block, message):
    with pytest.raises(ProfileError, match=message):
        load_with(tmp_path, block)


def test_an_ingress_overrides_one_effort_or_clears_the_declaration(tmp_path):
    block = "{pattern: '(\\w+)', efforts: {low: Low, medium: Low}}"
    override = "    capabilities:\n      effort_rendering: {efforts: {medium: Max}}\n"
    profile = load_with(tmp_path, block, override)
    assert profile.ingresses[0].capabilities.effort_rendering.efforts == {
        "low": "Low",
        "medium": "Max",
    }
    cleared = load_with(tmp_path, block, "    capabilities: {effort_rendering: null}\n")
    assert cleared.ingresses[0].capabilities.effort_rendering is None
