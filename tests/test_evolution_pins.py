"""The prompt-pin cases against a fleet whose render drifts behind a clean 200."""

from __future__ import annotations

import json

import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from llmdv.surfaces import base as surfaces
from llmdv.surfaces import variants
from tests.evolution_helpers import failed, run_evolution
from tests.fakes import MODEL, FakeFleet, rendered_size
from tests.test_suites_against_fake import write_profile

PIN = "evolution.prompt_pin"
TOOLS = "evolution.prompt_pin_tools"
CONVERSATION_SURFACES = ("chat_completions", "completions", "messages", "responses")
TOOL_SURFACES = ("chat_completions", "messages", "responses")


def expected(surface: str, pin: str) -> int:
    """What the faithful fake renders for the canonical body: the value an operator declares."""
    body = variants.canonical_body(surfaces.get(surface), MODEL, 256, pin == "tools")
    return rendered_size(json.dumps(body).encode(), None)


def pins(offset: int = 0) -> dict:
    out = {s: {"conversation": expected(s, "conversation") + offset} for s in CONVERSATION_SURFACES}
    for surface in TOOL_SURFACES:
        out[surface]["tools"] = expected(surface, "tools") + offset
    return out


def declare(tmp_path, fleet, prompt_pins):
    path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"]["prompt_pins"] = prompt_pins
    raw["model"]["tools"] = {"parser": "example"}
    path.write_text(yaml.safe_dump(raw))
    return path


def run(tmp_path, fleet, prompt_pins):
    profile = declare(tmp_path, fleet, prompt_pins)
    return run_evolution(profile, tmp_path / "run", "test_prompt_pin")


def outcomes(records):
    return {surface: r["outcome"] for surface, r in records.items()}


def test_faithful_render_matches_every_declared_pin(tmp_path):
    with FakeFleet(byte_probe=True) as fleet:
        code, by_case = run(tmp_path, fleet, pins())
    assert code == 0, by_case
    assert outcomes(by_case[PIN]) == dict.fromkeys(CONVERSATION_SURFACES, "pass")
    assert {s: o for s, o in outcomes(by_case[TOOLS]).items() if o == "pass"} == dict.fromkeys(
        TOOL_SURFACES, "pass"
    )
    assert set(by_case[TOOLS]) == set(TOOL_SURFACES)


def test_canonical_conversation_is_sent_in_each_api_shape(tmp_path):
    with FakeFleet(byte_probe=True) as fleet:
        run(tmp_path, fleet, pins())
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    sent = {
        (e["case_id"], e["url"].rsplit("/v1/", 1)[-1]): e["request_body"]
        for e in map(json.loads, lines)
    }
    chat = sent[(PIN, "chat/completions")]
    assert [m["role"] for m in chat["messages"]] == ["system", "user", "assistant", "user"]
    messages = sent[(TOOLS, "messages")]
    assert messages["system"] == variants.CANONICAL_SYSTEM
    assert messages["tools"][0]["name"] == "llmdv_lookup_color"
    assert messages["tool_choice"] == {
        "type": "tool",
        "name": "llmdv_lookup_color",
        "disable_parallel_tool_use": True,
    }
    assert "temperature" not in messages
    responses = sent[(PIN, "responses")]
    assert responses["instructions"] == variants.CANONICAL_SYSTEM
    assert [item["role"] for item in responses["input"]] == ["user", "assistant", "user"]
    assert sent[(TOOLS, "chat/completions")]["tool_choice"] == {
        "type": "function",
        "function": {"name": "llmdv_lookup_color"},
    }
    assert sent[(TOOLS, "responses")]["tool_choice"] == {
        "type": "function",
        "name": "llmdv_lookup_color",
    }


@pytest.mark.parametrize(
    "defect,failing",
    [
        ("drops_system", {"chat_completions", "messages", "responses"}),
        ("template_line", set(CONVERSATION_SURFACES)),
    ],
)
def test_render_drift_fails_the_pin(tmp_path, defect, failing):
    with FakeFleet(byte_probe=True, byte_defect=defect) as fleet:
        code, by_case = run(tmp_path, fleet, pins())
    assert code == 1
    assert {s for s, o in outcomes(by_case[PIN]).items() if o == "fail"} == failing
    assert all(failed(by_case[PIN][s], "prompt_pin") for s in failing)


def test_a_pin_measures_the_prompt_not_the_reply(tmp_path):
    """A model may end the cue-less Completions conversation at once; the pin still holds."""
    with FakeFleet(byte_probe=True, cueless_eos=True) as fleet:
        code, by_case = run(tmp_path, fleet, pins())
    assert code == 0, by_case
    record = by_case[PIN]["completions"]
    assert record["outcome"] == "pass", record
    chars = next(c for c in record["checks"] if c["name"] == "output_chars")
    assert chars["observed"].startswith("content=0"), chars


def test_a_drifted_render_fails_the_pin_on_an_empty_reply(tmp_path):
    with FakeFleet(byte_probe=True, byte_defect="template_line", cueless_eos=True) as fleet:
        code, by_case = run(tmp_path, fleet, pins())
    assert code == 1
    assert by_case[PIN]["completions"]["outcome"] == "fail"
    assert failed(by_case[PIN]["completions"], "prompt_pin")


def test_a_stale_declaration_fails_in_either_direction(tmp_path):
    with FakeFleet(byte_probe=True) as fleet:
        code, by_case = run(tmp_path, fleet, pins(offset=-1))
    assert code == 1
    assert set(outcomes(by_case[PIN]).values()) == {"fail"}


def test_undeclared_pins_skip_without_a_request(tmp_path):
    with FakeFleet(byte_probe=True) as fleet:
        code, by_case = run(tmp_path, fleet, {})
        assert not [s for s in fleet.seen if s[0] == "POST"]
    assert code == 0
    assert {r["skip_class"] for r in by_case[PIN].values()} == {"undeclared"}


@pytest.mark.parametrize(
    "value,message",
    [
        ({"chat_render_x": {"conversation": 10}}, "unknown surface"),
        ({"chat_completions": {"history": 10}}, "unknown pin"),
        ({"chat_completions": {"conversation": 0}}, "positive integer"),
        ({"chat_completions": {"conversation": "12"}}, "positive integer"),
    ],
)
def test_loader_rejects_malformed_declarations(tmp_path, value, message):
    with FakeFleet() as fleet:
        path = declare(tmp_path, fleet, value)
    with pytest.raises(ProfileError, match=message):
        load(path)
