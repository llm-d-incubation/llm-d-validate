"""The byte-fidelity cases against a fleet whose hops re-encode the request."""

from __future__ import annotations

import json

import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from tests.evolution_helpers import failed, run_evolution
from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile

ALL = ["big_integer", "big_integer_history", "key_order", "unicode_escapes"]
CASES = {
    "big_integer": "evolution.big_integer",
    "big_integer_history": "evolution.big_integer_history",
    "key_order": "evolution.key_order",
    "unicode_escapes": "evolution.unicode_escapes",
}
SURFACES = {
    "big_integer": {"chat_completions", "messages", "responses"},
    "big_integer_history": {"messages"},
    "key_order": {"chat_completions", "messages", "responses"},
    "unicode_escapes": {"chat_completions", "completions", "messages", "responses"},
}


def declare(tmp_path, fleet, probes):
    path = write_profile(tmp_path, fleet, structured_output=True)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"]["byte_fidelity"] = probes
    raw["model"]["tools"] = {"parser": "example"}
    path.write_text(yaml.safe_dump(raw))
    return path


def run(tmp_path, fleet, probes=ALL):
    profile = declare(tmp_path, fleet, probes)
    return run_evolution(profile, tmp_path / "run", "test_byte_fidelity")


def test_faithful_fleet_passes_every_probe_on_every_surface(tmp_path):
    with FakeFleet(byte_probe=True) as fleet:
        code, by_case = run(tmp_path, fleet)
    assert code == 0, by_case
    for probe, case in CASES.items():
        passed = {s for s, r in by_case[case].items() if r["outcome"] == "pass"}
        assert passed == SURFACES[probe], (probe, passed)


def test_probes_send_the_integer_and_the_escaped_spelling(tmp_path):
    with FakeFleet(byte_probe=True) as fleet:
        run(tmp_path, fleet, ["big_integer", "big_integer_history", "unicode_escapes"])
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    exchanges = [json.loads(line) for line in lines]
    chat = [e for e in exchanges if e["url"].endswith("/v1/chat/completions")]
    exact, rounded = [e for e in chat if e["case_id"] == CASES["big_integer"]]
    bound = exact["request_body"]["tools"][0]["function"]["parameters"]["properties"]["count"]
    assert bound["maximum"] == 12345678901234567890123
    assert rounded["request_body"]["tools"][0]["function"]["parameters"]["properties"]["count"][
        "maximum"
    ] == pytest.approx(1.2345678901234568e22)
    assert all(
        exchange["request_body"]["tool_choice"]
        == {"type": "function", "function": {"name": "llmdv_set_count"}}
        for exchange in (exact, rounded)
    )
    messages = [
        e
        for e in exchanges
        if e["case_id"] in (CASES["big_integer"], CASES["big_integer_history"])
        and e["url"].endswith("/v1/messages")
    ]
    assert all(exchange["request_body"]["tool_choice"]["type"] == "tool" for exchange in messages)
    responses = [
        e
        for e in exchanges
        if e["case_id"] == CASES["big_integer"] and e["url"].endswith("/v1/responses")
    ]
    assert all(
        exchange["request_body"]["tool_choice"] == {"type": "function", "name": "llmdv_set_count"}
        for exchange in responses
    )
    plain, escaped = [e for e in chat if e["case_id"] == CASES["unicode_escapes"]]
    assert "\U0001f680" in plain["request_body"] and "\\ud83d\\ude80" in escaped["request_body"]


@pytest.mark.parametrize(
    "defect,probe,check",
    [
        ("float64", "big_integer", "big_integer_digits"),
        ("float64", "big_integer_history", "big_integer_digits"),
        ("sorted_keys", "key_order", "output_key_order"),
        ("escape_mangle", "unicode_escapes", "unicode_escapes"),
    ],
)
def test_each_re_encoding_fails_its_probe_on_every_surface(tmp_path, defect, probe, check):
    with FakeFleet(byte_probe=True, byte_defect=defect) as fleet:
        code, by_case = run(tmp_path, fleet, [probe])
    assert code == 1
    records = by_case[CASES[probe]]
    assert {s for s, r in records.items() if r["outcome"] == "fail"} == SURFACES[probe]
    assert all(failed(r, check) for r in records.values() if r["outcome"] == "fail")


def test_a_re_encoding_hop_passes_the_ordinary_structured_output_case(tmp_path):
    """Sorted keys still satisfy the schema, which is why key order needs its own case."""
    with FakeFleet(byte_probe=True, byte_defect="sorted_keys") as fleet:
        profile = declare(tmp_path, fleet, [])
        code, records = run_suites(
            profile, tmp_path / "run", "-k", "test_structured_output and not stream"
        )
    assert code == 0
    schema = [r for r in records if r.get("case_id") == "field_options.structured_output"]
    assert {r["surface"] for r in schema if r["outcome"] == "pass"} == SURFACES["key_order"]


@pytest.mark.parametrize("declared,outcome", [(True, "fail"), (False, "pass")])
def test_call_markup_in_the_answer_fails_a_probe_that_judges_only_tokens(
    tmp_path, declared, outcome
):
    """The replayed-call probe compares input tokens only, yet a call the parser handed back
    as text is still a leak, judged by the shared contract wherever the markup is declared."""
    leaked = {"content": [{"type": "text", "text": "<tool_call>llmdv_lookup_color</tool_call>"}]}
    with FakeFleet(byte_probe=True, message_overrides=leaked) as fleet:
        profile = declare(tmp_path, fleet, ["big_integer_history"])
        if declared:
            raw = yaml.safe_load(profile.read_text())
            raw["model"]["tools"]["markup"] = ["<tool_call>"]
            profile.write_text(yaml.safe_dump(raw))
        code, by_case = run_evolution(profile, tmp_path / "run", "test_byte_fidelity")
    assert code == (1 if declared else 0)
    record = by_case[CASES["big_integer_history"]]["messages"]
    assert record["outcome"] == outcome
    assert failed(record, "tool_markup_in_answer") is declared


def test_undeclared_probes_skip_without_a_request(tmp_path):
    with FakeFleet(byte_probe=True) as fleet:
        code, by_case = run(tmp_path, fleet, [])
        assert not [s for s in fleet.seen if s[0] == "POST"]
    assert code == 0
    for case in CASES.values():
        assert {r["skip_class"] for r in by_case[case].values()} == {"undeclared"}


@pytest.mark.parametrize(
    "value,message",
    [(["big_integer", "big_integer"], "distinct"), (["floats"], "unknown probe"), ("all", "list")],
)
def test_loader_rejects_malformed_declarations(tmp_path, value, message):
    with FakeFleet() as fleet:
        path = declare(tmp_path, fleet, value)
    with pytest.raises(ProfileError, match=message):
        load(path)
