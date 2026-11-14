"""The contract drift cases against a fleet whose release changes a response shape."""

from __future__ import annotations

import json

import pytest
import yaml

from llmdv.assertions.contract import baseline, compare, shape, stream_shape
from llmdv.compare import diff
from llmdv.compare.archive import read_local_results
from llmdv.http.sse import read
from llmdv.profile.loader import ProfileError, load
from tests.evolution_helpers import failed, run_evolution
from tests.fakes import FakeFleet, message_events, named_sse
from tests.test_suites_against_fake import write_profile

SURFACES = ("chat_completions", "completions", "messages", "responses")
CASES = {
    "response": "evolution.response_contract",
    "stream": "evolution.stream_contract",
    "error": "evolution.error_contract",
    "invalid": "evolution.invalid_contract",
    "tool_response": "evolution.tool_response_contract",
    "tool_stream": "evolution.tool_stream_contract",
}
TOOL_SURFACES = {"chat_completions", "messages", "responses"}
TOOLS = {"chat_tools": True, "message_tools": True, "response_tools": True}
# A seed baseline every fleet satisfies, for the first run that records the real shapes.
SEED = {s: {part: {"llmdv_seed": ["absent"]} for part in CASES} for s in SURFACES}


def declare(tmp_path, fleet, baselines):
    path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"]["contract_baseline"] = baselines
    raw["model"]["tools"] = {"parser": "example"}
    path.write_text(yaml.safe_dump(raw))
    return path


def run(tmp_path, fleet, baselines):
    profile = declare(tmp_path, fleet, baselines)
    return run_evolution(profile, tmp_path / "run", "test_contract")


def recorded_baselines(tmp_path) -> dict:
    """What an operator does: derive each baseline from a run against a known-good build."""
    out: dict = {s: {} for s in SURFACES}
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    for exchange in map(json.loads, lines):
        surface = exchange["surface"]
        part = next(p for p, case in CASES.items() if case == exchange["case_id"])
        body = exchange["response_body"]
        if part in ("stream", "tool_stream"):
            frames = [e.json() for e in read(body.splitlines()).data_events]
            observed = stream_shape(frames)
        else:
            observed = shape(body)
        out[surface][part] = baseline(observed)
    return out


@pytest.fixture(scope="module")
def baselines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("seed")
    with FakeFleet(**TOOLS) as fleet:
        code, _ = run(tmp_path, fleet, SEED)
    assert code == 0
    return recorded_baselines(tmp_path)


def failing(by_case, part):
    return {s for s, r in by_case[CASES[part]].items() if r["outcome"] == "fail"}


def test_recorded_baselines_name_the_fields_clients_read(baselines):
    chat = baselines["chat_completions"]
    assert chat["response"]["choices[].message.reasoning_content"] == ["null"]
    assert chat["stream"]["usage.prompt_tokens"] == ["integer"]
    assert baselines["messages"]["stream"]["message_start:message.usage.input_tokens"] == [
        "integer"
    ]
    assert baselines["messages"]["stream"]["message_start:"] == ["object"]
    assert baselines["responses"]["error"]["error.code"] == ["integer"]
    assert baselines["messages"]["invalid"]["error.type"] == ["string"]
    assert baselines["chat_completions"]["tool_response"][
        "choices[].message.tool_calls[].function.arguments"
    ] == ["string"]
    assert baselines["messages"]["tool_response"]["content[].input"] == ["object"]


def test_unchanged_fleet_passes_its_own_baseline(tmp_path, baselines):
    with FakeFleet(**TOOLS) as fleet:
        code, by_case = run(tmp_path, fleet, baselines)
    assert code == 0, by_case
    for case in CASES.values():
        assert {r["outcome"] for r in by_case[case].values()} == {"pass"}, case


def test_a_new_field_passes_and_is_recorded(tmp_path, baselines):
    with FakeFleet(chat_overrides={"service_tier": "default"}, **TOOLS) as fleet:
        code, by_case = run(tmp_path, fleet, baselines)
    assert code == 0
    record = by_case[CASES["response"]]["chat_completions"]
    new = next(c for c in record["checks"] if c["name"] == "response_new_paths")
    assert "service_tier" in new["observed"]


def test_stream_contract_rejects_an_aborted_generation(tmp_path, baselines):
    with FakeFleet(abort_finish_reason=True, **TOOLS) as fleet:
        code, by_case = run(tmp_path, fleet, baselines)
    assert code == 1
    assert failing(by_case, "stream") == set(SURFACES)
    checks = dict.fromkeys(SURFACES, "finish_reason")
    checks["responses"] = "contract_missing"
    assert all(
        failed(by_case[CASES["stream"]][surface], check) for surface, check in checks.items()
    )


def test_stream_contract_requires_the_openai_done_sentinel(tmp_path, baselines):
    with FakeFleet(omit_done=True, **TOOLS) as fleet:
        code, by_case = run(tmp_path, fleet, baselines)
    assert code == 1
    assert failing(by_case, "stream") == {"chat_completions", "completions"}
    assert all(
        failed(by_case[CASES["stream"]][surface], "terminal_done")
        for surface in ("chat_completions", "completions")
    )


@pytest.mark.parametrize(
    "flag,check",
    [("usage_before_finish", "usage_last"), ("two_finish_reasons", "finish_frames")],
)
def test_completions_stream_contract_checks_frame_order(tmp_path, baselines, flag, check):
    with FakeFleet(**{flag: True}, **TOOLS) as fleet:
        code, by_case = run(tmp_path, fleet, baselines)
    assert code == 1
    record = by_case[CASES["stream"]]["completions"]
    assert record["outcome"] == "fail"
    assert failed(record, check)


def test_messages_stream_contract_stops_before_a_trailing_done(tmp_path, baselines):
    stream = named_sse(message_events()) + "data: [DONE]\n\n"
    declared = {"messages": {"stream": baselines["messages"]["stream"]}}
    with FakeFleet(messages_stream=stream, **TOOLS) as fleet:
        code, by_case = run(tmp_path, fleet, declared)
    assert code == 0, by_case
    assert by_case[CASES["stream"]]["messages"]["outcome"] == "pass"


@pytest.mark.parametrize(
    "defect,part,check,surfaces",
    [
        ("renamed", "response", "contract_missing", {"chat_completions", "messages", "responses"}),
        ("stream_renamed", "stream", "contract_missing", set(SURFACES)),
        ("error_renamed", "error", "contract_missing", set(SURFACES)),
        ("error_retyped", "error", "contract_retyped", set(SURFACES)),
        ("error_renamed", "invalid", "contract_missing", set(SURFACES)),
        ("tool_args_retyped", "tool_response", "contract_retyped", TOOL_SURFACES),
        ("stream_renamed", "tool_stream", "contract_missing", TOOL_SURFACES),
    ],
)
def test_a_changed_shape_fails_and_names_the_field(
    tmp_path, baselines, defect, part, check, surfaces
):
    with FakeFleet(contract_defect=defect, **TOOLS) as fleet:
        code, by_case = run(tmp_path, fleet, baselines)
    assert code == 1
    assert failing(by_case, part) == surfaces
    for surface in surfaces:
        record = by_case[CASES[part]][surface]
        assert failed(record, check), record["checks"]
        deciding = next(c for c in record["checks"] if c["name"] == check)
        assert deciding["observed"]
        if defect == "stream_renamed":
            assert "usage" in deciding["observed"]


def test_a_nightly_comparison_names_the_case_and_its_record_names_the_field(tmp_path, baselines):
    with FakeFleet(**TOOLS) as fleet:
        run(tmp_path / "before", fleet, baselines)
    with FakeFleet(contract_defect="renamed", **TOOLS) as fleet:
        run(tmp_path / "after", fleet, baselines)
    after = read_local_results(tmp_path / "after" / "run")
    delta = diff.compare(read_local_results(tmp_path / "before" / "run"), after)
    assert any(
        name.startswith("evolution.response_contract[chat_completions]")
        for name in delta.newly_failing
    ), delta.newly_failing
    record = next(
        r for r in after if r["case_id"] == CASES["response"] and r["surface"] == "chat_completions"
    )
    assert "choices[].message.reasoning_content" in record["reason"]


def test_compare_binds_children_only_under_a_present_parent():
    declared = {
        "a": ["object", "null"],
        "a.b": ["string"],
        "e:": ["object"],
        "e:x": ["integer"],
        "gone": ["string", "absent"],
    }
    assert compare(shape({"a": None}) | stream_shape([{"type": "e", "x": 1}]), declared) == (
        [],
        [],
        ["e:type"],
    )
    assert compare(shape({"a": {}}), declared)[0] == ["a.b", "e:"]
    assert compare(shape({"a": {"b": 1}}), declared)[1] == ["a.b (integer)"]


def test_undeclared_parts_skip_without_a_request(tmp_path):
    with FakeFleet(**TOOLS) as fleet:
        code, by_case = run(tmp_path, fleet, {})
        assert not [s for s in fleet.seen if s[0] == "POST"]
    assert code == 0
    for case in CASES.values():
        assert {r["skip_class"] for r in by_case[case].values()} == {"undeclared"}


@pytest.mark.parametrize(
    "value,message",
    [
        ({"chat_completions": {"body": {"id": ["string"]}}}, "unknown part"),
        ({"chat_completions": {"response": {}}}, "non-empty"),
        ({"chat_completions": {"response": {"id": ["text"]}}}, "types from"),
        ({"nowhere": {"response": {"id": ["string"]}}}, "unknown surface"),
    ],
)
def test_loader_rejects_malformed_declarations(tmp_path, value, message):
    with FakeFleet(**TOOLS) as fleet:
        path = declare(tmp_path, fleet, value)
    with pytest.raises(ProfileError, match=message):
        load(path)
