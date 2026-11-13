"""The forward-compatibility cases against a fleet that mishandles unknown request elements."""

from __future__ import annotations

import json

import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from tests.evolution_helpers import failed, run_evolution
from tests.fakes import FakeFleet
from tests.test_suites_against_fake import write_profile

HEALTHY = {
    "unknown_message_field": "accepted",
    "unknown_block": "accepted",
    "unknown_tool_type": "rejected",
    "unknown_header": "accepted",
    "unknown_beta": "accepted",
    "unknown_item": "accepted",
    "count_tokens_block": "accepted",
    "unknown_enum_value": {
        "chat_completions": "accepted",
        "completions": "accepted",
        "messages": "rejected",
        "responses": "accepted",
    },
}
CASES = {
    "unknown_message_field": "evolution.unknown_message_field",
    "unknown_block": "evolution.unknown_block",
    "unknown_tool_type": "evolution.unknown_tool_type",
    "unknown_header": "evolution.unknown_header",
    "unknown_beta": "evolution.unknown_beta",
    "unknown_item": "evolution.unknown_item",
    "count_tokens_block": "evolution.count_tokens_block",
    "unknown_enum_value": "evolution.unknown_enum_value",
}


def declare(tmp_path, fleet, forward_compat):
    path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"]["forward_compat"] = forward_compat
    path.write_text(yaml.safe_dump(raw))
    return path


def run(tmp_path, fleet, forward_compat=None, select="test_forward_compat"):
    profile = declare(tmp_path, fleet, HEALTHY if forward_compat is None else forward_compat)
    return run_evolution(profile, tmp_path / "run", select)


def requests(tmp_path):
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_healthy_fleet_passes_every_declared_probe_and_its_follow_up(tmp_path):
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet)
    assert code == 0, by_case
    for probe, case in CASES.items():
        outcomes = {surface: r["outcome"] for surface, r in by_case[case].items()}
        assert "pass" in outcomes.values(), (probe, outcomes)
        for surface, record in by_case[case].items():
            if record["outcome"] == "pass":
                names = [c["name"] for c in record["checks"]]
                assert "follow_up_status" in names, (case, surface)
    # Completions cannot carry a nested field, so it skips as not implemented.
    completions = by_case[CASES["unknown_block"]]["completions"]
    assert completions["outcome"] == "skip" and completions["skip_class"] == "not_implemented"


def test_probes_carry_one_unknown_element_in_each_api_shape(tmp_path):
    with FakeFleet() as fleet:
        run(tmp_path, fleet)
    sent = {}
    for exchange in requests(tmp_path):
        sent.setdefault((exchange["case_id"], exchange["url"].rsplit("/v1/", 1)[-1]), []).append(
            exchange
        )
    probe, follow = sent[("evolution.unknown_block", "messages")]
    assert probe["request_body"]["messages"][0]["content"][1]["type"] == "llmdv_future_block"
    assert follow["request_body"]["messages"][0]["content"] == "Reply with the single word: ok"
    probe, _ = sent[("evolution.unknown_message_field", "responses")]
    assert "llmdv_future_field" in probe["request_body"]["input"][0]
    probe, _ = sent[("evolution.unknown_header", "chat/completions")]
    assert probe["request_headers"]["x-llmdv-future-header"] == "1"
    (probe, _) = sent[("evolution.unknown_beta", "messages")]
    assert probe["request_headers"]["anthropic-beta"] == "llmdv-future-2099-01-01"
    rejected, follow = sent[("evolution.unknown_tool_type", "messages")]
    assert rejected["request_body"]["tools"][0]["type"] == "llmdv_future_tool_20990101"
    assert rejected["status"] == 400 and "tools" not in follow["request_body"]
    probe, _ = sent[("evolution.unknown_enum_value", "messages")]
    assert probe["request_body"]["thinking"] == {"type": "llmdv_future_value"}
    probe, follow = sent[("evolution.count_tokens_block", "messages/count_tokens")]
    assert probe["request_body"]["messages"][0]["content"][1]["type"] == "llmdv_future_block"
    assert "max_tokens" not in probe["request_body"] and follow["status"] == 200


def test_a_count_only_server_error_spares_the_generation_probe(tmp_path):
    declared = {"unknown_block": "accepted", "count_tokens_block": "accepted"}
    with FakeFleet(forward_defect="count_5xx") as fleet:
        code, by_case = run(tmp_path, fleet, declared)
    assert code == 1
    assert by_case[CASES["unknown_block"]]["messages"]["outcome"] == "pass"
    assert by_case[CASES["count_tokens_block"]]["messages"]["outcome"] == "fail"


@pytest.mark.parametrize(
    "defect,probe,check",
    [
        ("block_5xx", "unknown_block", "probe_status"),
        ("tool_5xx", "unknown_tool_type", "probe_status"),
        ("beta_5xx", "unknown_beta", "probe_status"),
        ("item_5xx", "unknown_item", "probe_status"),
        ("count_5xx", "count_tokens_block", "probe_status"),
        ("value_5xx", "unknown_enum_value", "probe_status"),
        ("header_502", "unknown_header", "probe_status"),
        ("drops_connection", "unknown_block", "probe_transport"),
        ("rejects_field", "unknown_message_field", "http_status"),
        ("accepts_tool", "unknown_tool_type", "probe_status"),
        ("crashes", "unknown_block", "follow_up_status"),
    ],
)
def test_each_mishandling_fails_the_probe_that_caused_it(tmp_path, defect, probe, check):
    with FakeFleet(forward_defect=defect) as fleet:
        code, by_case = run(tmp_path, fleet, {probe: HEALTHY[probe]})
    assert code == 1
    records = by_case[CASES[probe]]
    failing = [r for r in records.values() if r["outcome"] == "fail"]
    assert failing, records
    if defect == "crashes":
        # The probe that killed the engine fails on its follow-up; later probes meet a dead one.
        assert failed(failing[0], check), failing[0]["checks"]
        assert all(failed(r, "probe_status") for r in failing[1:])
    else:
        assert all(failed(r, check) for r in failing), [r["checks"] for r in failing]


def test_either_accepts_a_clean_refusal_and_still_fails_a_server_error(tmp_path):
    with FakeFleet() as fleet:
        code, _ = run(tmp_path / "clean", fleet, {"unknown_tool_type": "either"})
    assert code == 0
    with FakeFleet(forward_defect="tool_5xx") as fleet:
        code, by_case = run(tmp_path / "broken", fleet, {"unknown_tool_type": "either"})
    assert code == 1
    assert all(r["outcome"] == "fail" for r in by_case[CASES["unknown_tool_type"]].values())


def test_a_rejection_in_another_api_envelope_still_counts_as_rejected(tmp_path):
    with FakeFleet(forward_defect="foreign_envelope") as fleet:
        code, by_case = run(tmp_path, fleet, {"unknown_tool_type": "rejected"})
    assert code == 0, by_case
    for record in by_case[CASES["unknown_tool_type"]].values():
        envelope = next(c for c in record["checks"] if c["name"] == "probe_error_envelope")
        assert envelope["observed"] == "not in this surface's shape"


def test_a_per_surface_outcome_holds_where_the_surfaces_differ(tmp_path):
    declared = {"unknown_tool_type": {"messages": "rejected", "chat_completions": "either"}}
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, declared)
    assert code == 0
    records = by_case[CASES["unknown_tool_type"]]
    assert {s: r["outcome"] for s, r in records.items()} == {
        "chat_completions": "pass",
        "messages": "pass",
        "responses": "skip",
    }
    assert records["responses"]["skip_class"] == "undeclared"


def test_undeclared_probes_skip_without_a_request(tmp_path):
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, {})
        posted = [s for s in fleet.seen if s[0] == "POST"]
    assert code == 0 and not posted
    for case in CASES.values():
        assert {r["skip_class"] for r in by_case[case].values()} == {"undeclared"}


@pytest.mark.parametrize(
    "value,message",
    [
        ({"unknown_widget": "accepted"}, "unknown probe"),
        ({"unknown_block": "ignored"}, "expected one of"),
        (["unknown_block"], "expected a mapping"),
        ({"unknown_block": {"chat_render_x": "accepted"}}, "unknown surface"),
        ({"unknown_block": {"messages": "ignored"}}, "unknown_block.messages: expected one of"),
        ({"unknown_block": {}}, "expected one of"),
    ],
)
def test_loader_rejects_malformed_declarations(tmp_path, value, message):
    with FakeFleet() as fleet:
        path = declare(tmp_path, fleet, value)
    with pytest.raises(ProfileError, match=message):
        load(path)
