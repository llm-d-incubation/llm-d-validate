"""The render-pin cases against a fleet whose render changes behind an unchanged length."""

from __future__ import annotations

import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from llmdv.suites.evolution.helpers import token_digest
from llmdv.surfaces import base as surfaces
from llmdv.surfaces import variants
from tests.evolution_helpers import failed, run_evolution
from tests.fakes import MODEL, FakeFleet, render_payload
from tests.test_suites_against_fake import write_profile

PIN = "evolution.render_pin"
TOOLS = "evolution.render_pin_tools"
SURFACES = ("chat_render", "completions_render", "messages_render", "responses_render")
TOOL_SURFACES = ("chat_render", "messages_render", "responses_render")


def expected(surface: str, pin: str) -> str:
    """What the faithful fake renders for the canonical body: the digest an operator declares."""
    render = surfaces.get(surface)
    body = variants.canonical_body(render.request_surface, MODEL, 256, pin == "tools")
    payload = render_payload(body, surfaces.SURFACE_PATHS[surface], MODEL, None, 1)
    payload = payload[0] if isinstance(payload, list) else payload
    return token_digest(payload["token_ids"])


def pins() -> dict:
    out = {s: {"conversation": expected(s, "conversation")} for s in SURFACES}
    for surface in TOOL_SURFACES:
        out[surface]["tools"] = expected(surface, "tools")
    return out


def declare(tmp_path, fleet, render_pins):
    path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["ingresses"][0]["surfaces"] = list(SURFACES)
    raw["clients"] = []
    raw["capabilities"]["render_pins"] = render_pins
    raw["model"]["tools"] = {"parser": "example"}
    path.write_text(yaml.safe_dump(raw))
    return path


def run(tmp_path, fleet, render_pins):
    profile = declare(tmp_path, fleet, render_pins)
    return run_evolution(profile, tmp_path / "run", "test_render_pin")


def outcomes(records):
    return {surface: r["outcome"] for surface, r in records.items()}


def test_faithful_render_matches_every_declared_digest(tmp_path):
    declared = pins()
    declared["completions_render"]["tools"] = "0" * 64
    with FakeFleet(render=True) as fleet:
        code, by_case = run(tmp_path, fleet, declared)
    assert code == 0, by_case
    assert outcomes(by_case[PIN]) == dict.fromkeys(SURFACES, "pass")
    assert outcomes(by_case[TOOLS]) == {
        **dict.fromkeys(TOOL_SURFACES, "pass"),
        "completions_render": "skip",
    }
    assert by_case[TOOLS]["completions_render"]["skip_class"] == "not_implemented"


def test_the_tools_pin_renders_the_tool(tmp_path):
    assert expected("chat_render", "tools") != expected("chat_render", "conversation")


@pytest.mark.parametrize(
    "defect,conversation,tools",
    [
        ("substituted", set(SURFACES), set(TOOL_SURFACES)),
        ("sorted_tools", set(), set(TOOL_SURFACES)),
    ],
)
def test_same_length_drift_fails_the_digest(tmp_path, defect, conversation, tools):
    with FakeFleet(render=True, render_defect=defect) as fleet:
        code, by_case = run(tmp_path, fleet, pins())
    assert code == 1
    assert {s for s, o in outcomes(by_case[PIN]).items() if o == "fail"} == conversation
    assert {s for s, o in outcomes(by_case[TOOLS]).items() if o == "fail"} == tools
    for surface in tools:
        record = by_case[TOOLS][surface]
        assert failed(record, "render_pin")
        length = next(c for c in record["checks"] if c["name"] == "canonical_render_length")
        assert length["observed"] == len(
            render_payload(
                variants.canonical_body(
                    surfaces.get(surface).request_surface, MODEL, 256, with_tools=True
                ),
                surfaces.SURFACE_PATHS[surface],
                MODEL,
                None,
                1,
            )["token_ids"]
        )


def test_undeclared_pins_skip_without_a_request(tmp_path):
    with FakeFleet(render=True) as fleet:
        code, by_case = run(tmp_path, fleet, {})
        assert not [s for s in fleet.seen if s[0] == "POST"]
    assert code == 0
    assert {r["skip_class"] for r in by_case[PIN].values()} == {"undeclared"}


@pytest.mark.parametrize(
    "value,message",
    [
        ({"chat_completions": {"conversation": "0" * 64}}, "not a render surface"),
        ({"chat_render": {"history": "0" * 64}}, "unknown pin"),
        ({"chat_render": {"conversation": "0" * 63}}, "SHA-256"),
        ({"chat_render": {"conversation": "A" * 64}}, "SHA-256"),
    ],
)
def test_loader_rejects_malformed_declarations(tmp_path, value, message):
    with FakeFleet() as fleet:
        path = declare(tmp_path, fleet, value)
    with pytest.raises(ProfileError, match=message):
        load(path)
