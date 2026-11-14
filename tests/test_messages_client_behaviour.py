"""The client-field, streaming, tool and layer cases against a fleet that misbehaves quietly."""

from __future__ import annotations

import json

import pytest
import yaml

from tests.test_messages_conversation import declare, failed_check, fake
from tests.test_suites_against_fake import run_suites

BLOCKS = "anthropic_messages.system_blocks_in_prompt"
BETA = "anthropic_messages.beta_header"
CONTEXT = "anthropic_messages.context_management"
EFFORT = "anthropic_messages.effort_effect"
STREAM = "anthropic_messages.incremental_stream"
PARALLEL = "anthropic_messages.parallel_tool_calls"
PARALLEL_STREAM = "anthropic_messages.streamed_parallel_tool_calls"
LAYERS = "anthropic_messages.prompt_size_matches_engine"
BETAS = ["interleaved-thinking-2025-05-14", "effort-2025-11-24"]


def run(tmp_path, fleet, select, tools=False, **capabilities):
    profile = declare(tmp_path, fleet, **capabilities)
    if tools:
        raw = yaml.safe_load(profile.read_text())
        raw["model"]["tools"] = {"parser": "example"}
        profile.write_text(yaml.safe_dump(raw))
    code, records = run_suites(profile, tmp_path / "run", "-k", select, "--llmdv-evidence", "all")
    return code, {r["case_id"]: r for r in records if r.get("case_id")}


def requests(tmp_path):
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.parametrize("defect,passes", [(None, True), ("drops_later_system_blocks", False)])
def test_later_system_blocks_must_reach_the_prompt(tmp_path, defect, passes):
    with fake(prompt_accounting=True, context_defect=defect) as fleet:
        code, by_case = run(tmp_path, fleet, "test_later_system_blocks", prompt_accounting=True)
    assert code == (0 if passes else 1)
    assert by_case[BLOCKS]["outcome"] == ("pass" if passes else "fail")
    assert failed_check(by_case[BLOCKS], "system_blocks_tokens") is not passes
    short, long = [e["request_body"]["system"] for e in requests(tmp_path)]
    assert len(short) == 1 and len(long) == 2
    assert "cache_control" in long[-1] and "cache_control" not in long[0]


@pytest.mark.parametrize("defect,passes", [(None, True), ("rejects_beta", False)])
def test_declared_beta_values_are_sent_together(tmp_path, defect, passes):
    with fake(client_field_defect=defect) as fleet:
        code, by_case = run(tmp_path, fleet, "test_declared_beta_values", anthropic_betas=BETAS)
    assert code == (0 if passes else 1)
    assert by_case[BETA]["outcome"] == ("pass" if passes else "fail")
    (exchange,) = requests(tmp_path)
    assert exchange["request_headers"]["anthropic-beta"] == ",".join(BETAS)


@pytest.mark.parametrize("defect,passes", [(None, True), ("rejects_context_management", False)])
def test_context_management_is_sent_with_its_beta_value(tmp_path, defect, passes):
    with fake(client_field_defect=defect) as fleet:
        code, by_case = run(tmp_path, fleet, "test_context_management", context_management=True)
    assert code == (0 if passes else 1)
    assert by_case[CONTEXT]["outcome"] == ("pass" if passes else "fail")
    (exchange,) = requests(tmp_path)
    assert exchange["request_body"]["context_management"]["edits"]
    assert "context-management" in exchange["request_headers"]["anthropic-beta"]


@pytest.mark.parametrize(
    "tokens,passes",
    [({"low": 100, "high": 300}, True), ({"low": 100, "high": 100}, False), (None, False)],
    ids=["honoured", "ignored", "unaware"],
)
def test_effort_must_change_output_size(tmp_path, tokens, passes):
    with fake(effort_tokens=tokens) as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_effort_changes_output_size",
            effort_effect=True,
            messages_efforts=["low", "high"],
        )
    assert code == (0 if passes else 1)
    assert by_case[EFFORT]["outcome"] == ("pass" if passes else "fail")
    assert failed_check(by_case[EFFORT], "effort_effect") is not passes
    sent = [e["request_body"]["output_config"]["effort"] for e in requests(tmp_path)]
    assert sent == ["low", "high"] * 3


def test_effort_case_needs_both_levels_declared(tmp_path):
    with fake() as fleet:
        _, by_case = run(
            tmp_path,
            fleet,
            "test_effort_changes_output_size",
            effort_effect=True,
            messages_efforts=["high"],
        )
        assert not [s for s in fleet.seen if s[0] == "POST"]
    assert by_case[EFFORT]["outcome"] == "skip" and by_case[EFFORT]["skip_class"] == "undeclared"


@pytest.mark.parametrize("gap,passes", [(0.3, True), (0.0, False)], ids=["relayed", "buffered"])
def test_buffered_stream_fails_the_delivery_check(tmp_path, gap, passes):
    with fake(messages_stream_gap_s=gap) as fleet:
        code, by_case = run(tmp_path, fleet, "test_stream_is_relayed", incremental_streaming=True)
    assert code == (0 if passes else 1)
    assert by_case[STREAM]["outcome"] == ("pass" if passes else "fail")
    assert failed_check(by_case[STREAM], "stream_delivery") is not passes


@pytest.mark.parametrize("case", [PARALLEL, PARALLEL_STREAM])
@pytest.mark.parametrize(
    "defect,check",
    [
        (None, None),
        ("single_call", "tool_calls_in_one_turn"),
        ("duplicate_id", "tool_calls_in_one_turn"),
        ("ignores_second_result", "tool_results_round_trip"),
    ],
)
def test_several_tool_calls_and_results_in_one_turn(tmp_path, case, defect, check):
    with fake(message_tools=True, message_tool_defect=defect) as fleet:
        code, by_case = run(
            tmp_path, fleet, "test_several_tool_calls", tools=True, parallel_tool_calls=True
        )
    record = by_case[case]
    assert record["outcome"] == ("pass" if defect is None else "fail"), record
    assert code == (0 if defect is None else 1)
    if check:
        assert failed_check(record, check)
    else:
        followups = [
            e["request_body"]["messages"][-1]["content"]
            for e in requests(tmp_path)
            if e["case_id"] == case and len(e["request_body"]["messages"]) == 3
        ]
        (results,) = followups
        assert [part["tool_use_id"] for part in results] == ["toolu_fake_0", "toolu_fake_1"]


def two_layers(tmp_path, engine, gateway):
    profile = declare(tmp_path, engine, prompt_accounting=True)
    raw = yaml.safe_load(profile.read_text())
    raw["ingresses"].append(
        {**raw["ingresses"][0], "id": "gateway", "kind": "litellm", "base_url": gateway.base_url}
    )
    profile.write_text(yaml.safe_dump(raw))
    code, records = run_suites(
        profile,
        tmp_path / "run",
        "-k",
        "test_prompt_size_matches_the_engine",
        "--llmdv-evidence",
        "all",
    )
    return code, {r["ingress"]: r for r in records}


@pytest.mark.parametrize("defect,passes", [(None, True), ("drops_mid_system", False)])
def test_a_layer_is_compared_with_the_engine_behind_it(tmp_path, defect, passes):
    with fake(prompt_accounting=True) as engine:
        with fake(prompt_accounting=True, context_defect=defect) as gateway:
            code, by_ingress = two_layers(tmp_path, engine, gateway)
    assert code == (0 if passes else 1)
    assert by_ingress["fake"]["outcome"] == "skip"
    assert by_ingress["gateway"]["outcome"] == ("pass" if passes else "fail")
    assert failed_check(by_ingress["gateway"], "prompt_size_matches_engine") is not passes


def test_a_single_ingress_has_nothing_to_compare_with(tmp_path):
    with fake(prompt_accounting=True) as fleet:
        code, by_case = run(
            tmp_path, fleet, "test_prompt_size_matches_the_engine", prompt_accounting=True
        )
    assert code == 0
    assert by_case[LAYERS]["outcome"] == "skip" and by_case[LAYERS]["skip_class"] == "undeclared"


def test_two_engines_leave_the_comparison_ambiguous(tmp_path):
    with fake(prompt_accounting=True) as engine, fake(prompt_accounting=True) as other:
        profile = declare(tmp_path, engine, prompt_accounting=True)
        raw = yaml.safe_load(profile.read_text())
        raw["ingresses"].append({**raw["ingresses"][0], "id": "pool-b", "base_url": other.base_url})
        raw["ingresses"].append(
            {**raw["ingresses"][0], "id": "gateway", "kind": "litellm", "base_url": other.base_url}
        )
        profile.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            profile, tmp_path / "run", "-k", "test_prompt_size_matches_the_engine"
        )
        assert not [s for s in engine.seen + other.seen if s[0] == "POST"]
    assert code == 0
    assert {r["skip_class"] for r in records} == {"undeclared"}


def test_stream_delivery_is_judged_against_the_stream_length():
    from llmdv.assertions.context import assert_incremental

    assert_incremental([0.30, 0.34, 0.38])  # a fast engine: short, but spread
    for buffered in ([2.000, 2.001], [0.0005, 0.0009], [1.0]):
        with pytest.raises(AssertionError, match="arrived together"):
            assert_incremental(buffered)
