"""Offline regressions for the spec-derived Chat and Completions contract cases.

Each case runs against `FakeFleet(openai_contract=True)`, a spec-faithful engine in
`tests/fake_openai.py`, and is proven to fail on the defect it exists to catch.
"""

from __future__ import annotations

import json

import pytest
import yaml

from llmdv.profile.schema import CHAT_FIELDS, COMPLETIONS_FIELDS
from llmdv.suites.openai_chat import fields as chat_fields
from llmdv.suites.openai_completions import fields as completions_fields
from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


def contract_records(tmp_path, selector, capabilities, surfaces=("chat_completions",), **fake):
    """Run the selected cases against a contract fake; return the exit code and records."""
    with FakeFleet(openai_contract=True, **fake) as fleet:
        path = write_profile(tmp_path, fleet, responses=False)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = list(surfaces)
        raw["capabilities"] = capabilities
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", selector)
    assert records, f"no results for {selector!r}"
    return code, records


def run_contract(tmp_path, selector, capabilities, surfaces=("chat_completions",), **fake):
    """Run the selected cases against a contract fake; return outcome by test node parameters."""
    code, records = contract_records(tmp_path, selector, capabilities, surfaces, **fake)
    return code, {r["nodeid"].split("::", 1)[1]: r["outcome"] for r in records}


def outcome(results, fragment):
    matches = [value for key, value in results.items() if fragment in key]
    assert len(matches) == 1, (fragment, results)
    return matches[0]


def test_field_probe_vocabularies_match_the_schema():
    assert set(chat_fields.names()) == set(CHAT_FIELDS)
    assert set(completions_fields.names()) == set(COMPLETIONS_FIELDS)
    assert chat_fields.STREAMED <= set(CHAT_FIELDS)


def test_every_declared_field_passes_on_a_healthy_fleet(tmp_path):
    chat = {name: "accepted" for name in CHAT_FIELDS}
    completions = {name: "ignored" for name in COMPLETIONS_FIELDS}
    chat["stream_options_alone"] = completions["stream_options_alone"] = "rejected"
    code, results = run_contract(
        tmp_path,
        "request_field",
        {"chat_fields": chat, "completions_fields": completions},
        surfaces=("chat_completions", "completions"),
        transport={"multimodal": True},
    )
    assert set(results.values()) == {"pass"}, results
    assert len(results) == len(CHAT_FIELDS) + len(COMPLETIONS_FIELDS)
    assert code == 0


def completions_accepted():
    declared = {name: "accepted" for name in COMPLETIONS_FIELDS}
    declared["stream_options_alone"] = "rejected"
    return {"completions_fields": declared}


def test_completions_fields_pass_where_a_raw_instruction_ends_at_once(tmp_path):
    """A model may end a raw, cue-less instruction at once; the probes send an open sentence."""
    code, results = run_contract(
        tmp_path,
        "test_completions_request_field",
        completions_accepted(),
        surfaces=("completions",),
        cueless_eos=True,
    )
    assert len(results) == len(COMPLETIONS_FIELDS)
    assert set(results.values()) == {"pass"}, results
    assert code == 0


def test_an_accepted_completions_field_that_empties_the_reply_fails(tmp_path):
    code, results = run_contract(
        tmp_path,
        "test_completions_request_field",
        completions_accepted(),
        surfaces=("completions",),
        cueless_eos=True,
        openai_contract_defect="seed_empties_reply",
    )
    assert outcome(results, "-seed]") == "fail"
    assert {k for k, v in results.items() if v == "fail"} == {
        k for k in results if k.endswith("-seed]")
    }
    assert code == 1
    lines = (tmp_path / "run" / "results.jsonl").read_text().splitlines()
    (record,) = [r for r in map(json.loads, lines) if r["nodeid"].endswith("-seed]")]
    assert any(c["name"] == "output_chars" and not c["ok"] for c in record["checks"]), record


def test_field_dispositions_are_falsifiable(tmp_path):
    declared = {"top_k": "accepted", "min_p": "rejected", "seed": "rejected"}
    code, results = run_contract(
        tmp_path,
        "test_chat_request_field and (top_k or min_p or seed or user)",
        {"chat_fields": declared},
        openai_field_status={"top_k": 500, "min_p": 400},
    )
    assert outcome(results, "-top_k]") == "fail"  # a 5xx under any declaration
    assert outcome(results, "-min_p]") == "pass"  # refused cleanly, as declared
    assert outcome(results, "-seed]") == "fail"  # declared rejected, silently accepted
    assert outcome(results, "-user]") == "skip"  # undeclared: no request
    assert code == 1


def test_a_refusal_without_an_envelope_fails(tmp_path):
    code, results = run_contract(
        tmp_path,
        "test_chat_request_field and min_p",
        {"chat_fields": {"min_p": "rejected"}},
        openai_field_status={"min_p": 400},
        plain_text_errors=True,
    )
    assert outcome(results, "-min_p]") == "fail"
    assert code == 1


@pytest.mark.parametrize("value", [{"top_k": "maybe"}, {"no_such_field": "accepted"}, ["top_k"]])
def test_field_declarations_are_validated(tmp_path, value):
    from llmdv.profile.loader import ProfileError, load

    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"] = {"chat_fields": value}
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ProfileError):
        load(path)


SHAPES = (
    "test_chat_response_fields or test_chat_stream_chunk_fields"
    " or test_completions_response_fields or test_completions_stream_chunk_fields"
)


def test_documented_fields_pass_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(
        tmp_path,
        SHAPES,
        {"openai_contract": True},
        surfaces=("chat_completions", "completions"),
    )
    assert len(results) == 6 and set(results.values()) == {"pass"}, results
    assert code == 0


def test_contract_cases_skip_where_undeclared(tmp_path):
    code, results = run_contract(
        tmp_path,
        SHAPES,
        {"openai_contract": ["completions"]},
        surfaces=("chat_completions", "completions"),
    )
    assert outcome(results, "test_chat_response_fields") == "skip"
    assert outcome(results, "test_completions_response_fields") == "pass"
    assert code == 0


@pytest.mark.parametrize(
    "defect,fragment",
    [
        ("created_ms", "test_chat_response_fields"),
        ("empty_id", "test_chat_response_fields"),
        ("finish_unknown", "test_chat_response_fields"),
        ("logprobs_unrequested", "test_chat_response_fields"),
        ("cached_over_prompt", "test_chat_response_fields"),
        ("tool_calls_without_tools", "test_chat_response_fields"),
        ("audio_unrequested", "test_chat_response_fields"),
        ("moderation_unrequested", "test_chat_response_fields"),
        ("prediction_over_completion", "test_chat_response_fields"),
        ("usage_chunk_choice", "test_chat_stream_chunk_fields[fake-usage]"),
        ("chunk_id_changes", "test_chat_stream_chunk_fields[fake-plain]"),
        ("no_role", "test_chat_stream_chunk_fields[fake-plain]"),
        ("reasoning_after_content", "test_chat_stream_chunk_fields[fake-plain]"),
        ("chunk_after_finish", "test_chat_stream_chunk_fields[fake-plain]"),
        ("usage_when_false", "test_chat_stream_chunk_fields[fake-usage_off]"),
        ("created_ms", "test_completions_response_fields"),
        ("cached_over_prompt", "test_completions_response_fields"),
        ("usage_chunk_choice", "test_completions_stream_chunk_fields"),
        ("completion_chunk_index_missing", "test_completions_stream_chunk_fields"),
        ("completion_chunk_index_bool", "test_completions_stream_chunk_fields"),
        ("completion_chunk_index_wrong", "test_completions_stream_chunk_fields"),
    ],
)
def test_documented_field_defects_fail(tmp_path, defect, fragment):
    code, results = run_contract(
        tmp_path,
        SHAPES,
        {"openai_contract": True},
        surfaces=("chat_completions", "completions"),
        openai_contract_defect=defect,
    )
    assert outcome(results, fragment) == "fail", results
    assert code == 1


LIMITS = "test_chat_token_limit or test_chat_single_token or test_chat_stop_sequence"
COMPLETIONS_LIMITS = "test_completions_token_limit or test_completions_stop_sequence"
# The fake model reasons by default, so the Chat stop cases need an effort that turns it off.
QUIET = {"chat_quiet_efforts": ["none"]}
STOPS = {"openai_contract": True, "vllm_options": ["include_stop_str_in_output"], **QUIET}


def test_limits_and_stops_pass_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(
        tmp_path,
        f"{LIMITS} or {COMPLETIONS_LIMITS}",
        STOPS,
        surfaces=("chat_completions", "completions"),
    )
    assert len(results) == 11 and set(results.values()) == {"pass"}, results
    assert code == 0


def test_included_stop_skips_where_undeclared(tmp_path):
    code, results = run_contract(
        tmp_path, "test_chat_stop_sequence and included", {"openai_contract": True, **QUIET}
    )
    assert set(results.values()) == {"skip"}, results


CHAT_STOPS = "test_chat_stop_sequence or (test_chat_stop_reason)"
REASONING_STOPS = {**STOPS, "vllm_options": ["include_stop_str_in_output", "stop_reason"]}


def test_chat_stops_turn_reasoning_off_on_a_reasoning_model(tmp_path):
    """A model that writes the count in its reasoning, where the engine matches the stop
    string, still passes: the cases send the declared quiet effort, so the stop meets the answer.
    """
    code, results = run_contract(tmp_path, CHAT_STOPS, REASONING_STOPS, openai_reasons=True)
    assert len(results) == 5 and set(results.values()) == {"pass"}, results
    assert code == 0
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    sent = [json.loads(line)["request_body"] for line in lines]
    assert sent and all(body.get("reasoning_effort") == "none" for body in sent), sent


def test_chat_stops_skip_on_a_reasoning_model_with_no_way_to_turn_it_off(tmp_path):
    caps = {k: v for k, v in REASONING_STOPS.items() if k != "chat_quiet_efforts"}
    code, results = run_contract(tmp_path, CHAT_STOPS, caps, openai_reasons=True)
    assert set(results.values()) == {"skip"}, results
    assert code == 0


def test_chat_stops_run_at_the_lowest_effort_on_a_model_with_no_off_state(tmp_path):
    """No quiet effort and no `none`, but declared efforts: the stop cases run at the lowest,
    with reasoning still on, rather than skipping."""
    caps = {k: v for k, v in REASONING_STOPS.items() if k != "chat_quiet_efforts"}
    caps["chat_reasoning_efforts"] = ["high", "low"]
    code, results = run_contract(tmp_path, CHAT_STOPS, caps, openai_reasons=True)
    assert len(results) == 5 and "skip" not in set(results.values()), results
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    sent = [json.loads(line)["request_body"] for line in lines]
    assert sent and all(body.get("reasoning_effort") == "low" for body in sent), sent


@pytest.mark.parametrize(
    "defect,failing",
    [
        ("stop_ignored", ["stop_sequence[fake-string]", "stop_sequence[fake-included]"]),
        ("stop_reason_missing", ["test_chat_stop_reason"]),
    ],
)
def test_chat_stop_defects_fail_with_reasoning_off(tmp_path, defect, failing):
    code, results = run_contract(
        tmp_path, CHAT_STOPS, REASONING_STOPS, openai_reasons=True, openai_contract_defect=defect
    )
    for fragment in failing:
        assert outcome(results, fragment) == "fail", (fragment, results)
    assert code == 1


@pytest.mark.parametrize(
    "defect,failing",
    [
        ("limit_ignored", ["token_limit[fake-max_tokens]", "single_token[fake-json]"]),
        ("max_tokens_wins", ["token_limit[fake-both]"]),
        ("length_as_stop", ["token_limit[fake-max_completion_tokens]", "single_token[fake-sse]"]),
        ("stop_ignored", ["stop_sequence[fake-string]", "stop_sequence[fake-list]"]),
        ("stop_ignored", ["stop_sequence[fake-sse]", "stop_sequence[fake-included]"]),
        ("stop_included", ["stop_sequence[fake-string]", "stop_sequence[fake-sse]"]),
        ("limit_ignored", ["test_completions_token_limit"]),
        ("stop_ignored", ["test_completions_stop_sequence"]),
    ],
)
def test_limit_and_stop_defects_fail(tmp_path, defect, failing):
    code, results = run_contract(
        tmp_path,
        f"{LIMITS} or {COMPLETIONS_LIMITS}",
        STOPS,
        surfaces=("chat_completions", "completions"),
        openai_contract_defect=defect,
    )
    for fragment in failing:
        assert outcome(results, fragment) == "fail", (fragment, results)
    assert code == 1


def test_an_included_stop_still_passes_when_the_engine_includes_it(tmp_path):
    code, results = run_contract(
        tmp_path,
        "test_chat_stop_sequence and included",
        STOPS,
        openai_contract_defect="stop_included",
    )
    assert set(results.values()) == {"pass"}, results


LOGPROBS = "test_chat_logprobs_per_token or test_completions_logprobs"
LOGPROB_CAPS = {"openai_contract": True, "chat_options": ["logprobs"]}


def test_logprobs_pass_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(
        tmp_path, LOGPROBS, LOGPROB_CAPS, surfaces=("chat_completions", "completions")
    )
    assert len(results) == 3 and set(results.values()) == {"pass"}, results
    assert code == 0


@pytest.mark.parametrize(
    "defect,failing",
    [
        ("logprobs_short", ["logprobs_per_token[fake-json]", "logprobs_per_token[fake-sse]"]),
        ("top_width", ["logprobs_per_token[fake-json]"]),
        ("text_logprobs_misaligned", ["test_completions_logprobs"]),
        ("text_logprobs_non_string_token", ["test_completions_logprobs"]),
        ("text_logprobs_string_score", ["test_completions_logprobs"]),
        ("text_logprobs_positive_score", ["test_completions_logprobs"]),
        ("text_logprobs_infinite_score", ["test_completions_logprobs"]),
    ],
)
def test_logprob_defects_fail(tmp_path, defect, failing):
    code, results = run_contract(
        tmp_path,
        LOGPROBS,
        LOGPROB_CAPS,
        surfaces=("chat_completions", "completions"),
        openai_contract_defect=defect,
    )
    for fragment in failing:
        assert outcome(results, fragment) == "fail", (fragment, results)
    assert code == 1


PROMPTS = (
    "test_completions_prompt_forms or test_completions_echo or test_completions_multiple_choices"
    " or test_completions_streaming_matches_json or test_completions_parse_with_pinned_sdk"
)
PROMPT_CAPS = {
    "openai_contract": ["completions"],
    "chat_options": ["n"],
    "chat_repeats": ["greedy"],
}


def test_prompt_forms_pass_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(tmp_path, PROMPTS, PROMPT_CAPS, surfaces=("completions",))
    assert len(results) == 7 and set(results.values()) == {"pass"}, results
    assert code == 0


@pytest.mark.parametrize(
    "defect,failing",
    [
        ("prompt_list_first_only", ["prompt_forms[fake-strings]", "[fake-token_id_lists]"]),
        ("token_ids_as_text", ["prompt_forms[fake-token_ids]", "[fake-token_id_lists]"]),
        ("echo_ignored", ["test_completions_echo"]),
        ("stream_text_differs", ["test_completions_streaming_matches_json"]),
        ("stream_prompt_count_differs", ["test_completions_streaming_matches_json"]),
        ("finish_unknown", ["test_completions_parse_with_pinned_sdk"]),
    ],
)
def test_prompt_form_defects_fail(tmp_path, defect, failing):
    code, results = run_contract(
        tmp_path,
        PROMPTS,
        PROMPT_CAPS,
        surfaces=("completions",),
        openai_contract_defect=defect,
    )
    for fragment in failing:
        assert outcome(results, fragment) == "fail", (fragment, results)
    assert code == 1


STREAM_PARITY = "test_completions_streaming_matches_json"


def _checks(record) -> dict[str, bool]:
    return {check["name"]: check["ok"] for check in record["checks"]}


@pytest.mark.parametrize("defect", ["stream_text_differs", "stream_prompt_count_differs"])
def test_completions_stream_divergence_fails_on_a_deterministic_fleet(tmp_path, defect):
    code, (record,) = contract_records(
        tmp_path,
        STREAM_PARITY,
        PROMPT_CAPS,
        surfaces=("completions",),
        openai_contract_defect=defect,
    )
    assert record["outcome"] == "fail", record
    assert _checks(record)["greedy_repeat"] is True
    assert _checks(record)["stream_matches_json"] is False
    assert code == 1


def test_completions_nondeterminism_fails_as_greedy_repeat_not_stream_parity(tmp_path):
    """Two JSON answers that differ blame the fleet's determinism; the stream is not judged."""
    code, (record,) = contract_records(
        tmp_path,
        STREAM_PARITY,
        PROMPT_CAPS,
        surfaces=("completions",),
        openai_contract_defect="greedy_unstable",
    )
    assert record["outcome"] == "fail", record
    checks = _checks(record)
    assert checks["greedy_repeat"] is False
    assert "stream_matches_json" not in checks
    assert code == 1


MESSAGES = "test_chat_developer_role or test_chat_content_parts or test_chat_unicode_round_trip"


def test_message_shapes_pass_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(tmp_path, MESSAGES, {"openai_contract": True})
    assert len(results) == 4 and set(results.values()) == {"pass"}, results
    assert code == 0


@pytest.mark.parametrize(
    "defect,failing",
    [
        ("drops_developer", ["test_chat_developer_role"]),
        ("first_part_only", ["test_chat_content_parts"]),
        ("mojibake", ["unicode_round_trip[fake-json]", "unicode_round_trip[fake-sse]"]),
    ],
)
def test_message_shape_defects_fail(tmp_path, defect, failing):
    code, results = run_contract(
        tmp_path, MESSAGES, {"openai_contract": True}, openai_contract_defect=defect
    )
    for fragment in failing:
        assert outcome(results, fragment) == "fail", (fragment, results)
    assert code == 1


ENGINE = (
    "test_chat_continue_final_message or test_chat_continuous_usage"
    " or test_chat_constrained_choice or test_chat_tokens_as_ids or test_chat_prompt_logprobs"
    " or test_chat_allowed_tokens or test_chat_bad_words or test_chat_logit_bias"
)
ENGINE_OPTIONS = [
    "continue_final_message",
    "echo",
    "continuous_usage_stats",
    "structured_outputs",
    "guided_choice",
    "return_tokens_as_token_ids",
    "prompt_logprobs",
    "allowed_token_ids",
    "bad_words",
    "logit_bias",
]


def test_engine_options_pass_on_a_healthy_fleet(tmp_path):
    caps = {"vllm_options": ENGINE_OPTIONS, "chat_options": ["logprobs"]}
    code, results = run_contract(tmp_path, ENGINE, caps)
    assert len(results) == 10 and set(results.values()) == {"pass"}, results
    assert code == 0


def test_engine_options_skip_where_undeclared(tmp_path):
    code, results = run_contract(tmp_path, ENGINE, {"vllm_options": ["continue_final_message"]})
    assert outcome(results, "continue_final_message[fake-plain]") == "pass"
    assert outcome(results, "continue_final_message[fake-echo]") == "skip"
    assert outcome(results, "test_chat_prompt_logprobs") == "skip"
    assert outcome(results, "test_chat_allowed_tokens") == "skip"
    assert code == 0


@pytest.mark.parametrize(
    "defect,failing",
    [
        (
            "continuation_in_reasoning",
            ["continue_final_message[fake-plain]", "continue_final_message[fake-echo]"],
        ),
        ("continuous_usage_ignored", ["test_chat_continuous_usage"]),
        ("continuous_usage_decreasing", ["test_chat_continuous_usage"]),
        (
            "structured_outputs_ignored",
            ["constrained_choice[fake-structured_outputs]", "[fake-guided_choice]"],
        ),
        ("token_strings", ["test_chat_tokens_as_ids"]),
        ("prompt_logprobs_short", ["test_chat_prompt_logprobs"]),
        ("allowed_tokens_ignored", ["test_chat_allowed_tokens"]),
        ("bad_words_ignored", ["test_chat_bad_words"]),
        ("logit_bias_ignored", ["test_chat_logit_bias"]),
    ],
)
def test_engine_option_defects_fail(tmp_path, defect, failing):
    caps = {"vllm_options": ENGINE_OPTIONS, "chat_options": ["logprobs"]}
    code, results = run_contract(tmp_path, ENGINE, caps, openai_contract_defect=defect)
    for fragment in failing:
        assert outcome(results, fragment) == "fail", (fragment, results)
    assert code == 1


REPEATS = "test_chat_seed_repeat or test_chat_greedy_repeat or test_chat_fresh_generation"
REPEAT_CAPS = {"chat_repeats": ["seed", "greedy", "fresh"]}


def test_repeats_pass_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(tmp_path, REPEATS, REPEAT_CAPS)
    assert len(results) == 3 and set(results.values()) == {"pass"}, results
    assert code == 0


@pytest.mark.parametrize(
    "defect,failing",
    [
        ("seed_ignored", "test_chat_seed_repeat"),
        ("greedy_unstable", "test_chat_greedy_repeat"),
        ("replay", "test_chat_fresh_generation"),
    ],
)
def test_repeat_defects_fail(tmp_path, defect, failing):
    code, results = run_contract(tmp_path, REPEATS, REPEAT_CAPS, openai_contract_defect=defect)
    assert outcome(results, failing) == "fail", results
    assert code == 1


FIDELITY = "test_chat_number_fidelity or test_chat_structured_key_order"
FIDELITY_CAPS = {"openai_contract": True, "structured_output": ["chat_completions"]}


def _with_tools(tmp_path, defect=None, auto_choice=False, select=FIDELITY):
    with FakeFleet(openai_contract=True, openai_contract_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet, responses=False)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["chat_completions"]
        raw["model"]["tools"] = {"parser": "fake", "auto_choice": auto_choice}
        raw["capabilities"] = FIDELITY_CAPS
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", select)
    return code, {r["nodeid"].split("::", 1)[1]: r["outcome"] for r in records}


def test_fidelity_passes_on_a_healthy_fleet(tmp_path):
    code, results = _with_tools(tmp_path)
    assert len(results) == 2 and set(results.values()) == {"pass"}, results
    assert code == 0


@pytest.mark.parametrize(
    "defect,failing",
    [("float_numbers", "test_chat_number_fidelity"), ("sorted_keys", "key_order")],
)
def test_fidelity_defects_fail(tmp_path, defect, failing):
    code, results = _with_tools(tmp_path, defect)
    assert outcome(results, failing) == "fail", results
    assert code == 1


@pytest.mark.parametrize(
    "auto_choice,defect,passes",
    [
        # A deployment that renders `tool_choice: none` without the tools: the case offers the
        # tool under `auto` where the model declares it, so the schema is still rendered.
        (True, "excludes_tools_on_none", True),
        (False, "excludes_tools_on_none", False),
        (True, "float_numbers", False),
    ],
)
def test_number_fidelity_renders_the_schema(tmp_path, auto_choice, defect, passes):
    code, results = _with_tools(tmp_path, defect, auto_choice, "test_chat_number_fidelity")
    assert outcome(results, "number_fidelity") == ("pass" if passes else "fail"), results
    assert code == (0 if passes else 1)
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    bodies = [json.loads(line)["request_body"] for line in lines]
    assert {body["tool_choice"] for body in bodies} == {"auto" if auto_choice else "none"}


THINKING = "test_chat_thinking_off or test_chat_reasoning_history"
THINKING_CAPS = {"openai_contract": True, "vllm_options": ["enable_thinking"]}


def test_thinking_controls_pass_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(tmp_path, THINKING, THINKING_CAPS)
    assert len(results) == 3 and set(results.values()) == {"pass"}, results
    assert code == 0


@pytest.mark.parametrize(
    "defect,failing",
    [
        ("thinking_leak", ["thinking_off[fake-json]", "thinking_off[fake-sse]"]),
        ("rejects_reasoning_history", ["test_chat_reasoning_history"]),
    ],
)
def test_thinking_control_defects_fail(tmp_path, defect, failing):
    code, results = run_contract(tmp_path, THINKING, THINKING_CAPS, openai_contract_defect=defect)
    for fragment in failing:
        assert outcome(results, fragment) == "fail", (fragment, results)
    assert code == 1


CUT = "test_chat_reasoning_cut_by_length"


def test_reasoning_cut_passes_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(tmp_path, CUT, {"openai_contract": True})
    assert len(results) == 2 and set(results.values()) == {"pass"}, results
    assert code == 0


@pytest.mark.parametrize("defect", ["cut_reasoning_in_content", "length_as_stop"])
def test_reasoning_cut_defects_fail(tmp_path, defect):
    code, results = run_contract(
        tmp_path, CUT, {"openai_contract": True}, openai_contract_defect=defect
    )
    assert len(results) == 2 and set(results.values()) == {"fail"}, results
    assert code == 1


ECHO_SCORE = "test_completions_scored_prompt"


def test_echo_logprobs_pass_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(
        tmp_path, ECHO_SCORE, {"openai_contract": True}, surfaces=("completions",)
    )
    assert set(results.values()) == {"pass"} and len(results) == 1, results
    assert code == 0


@pytest.mark.parametrize("defect", ["echo_logprobs_short", "echo_ignored"])
def test_echo_logprob_defects_fail(tmp_path, defect):
    code, results = run_contract(
        tmp_path,
        ECHO_SCORE,
        {"openai_contract": True},
        surfaces=("completions",),
        openai_contract_defect=defect,
    )
    assert set(results.values()) == {"fail"} and len(results) == 1, results
    assert code == 1


NULLS = "null_optional_fields"


def test_null_optional_fields_pass_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(
        tmp_path, NULLS, {"openai_contract": True}, surfaces=("chat_completions", "completions")
    )
    assert set(results.values()) == {"pass"} and len(results) == 2, results
    assert code == 0


def test_null_optional_fields_fail_on_a_crash(tmp_path):
    code, results = run_contract(
        tmp_path,
        NULLS,
        {"openai_contract": True},
        surfaces=("chat_completions", "completions"),
        openai_contract_defect="null_crash",
    )
    assert set(results.values()) == {"fail"} and len(results) == 2, results
    assert code == 1


SDK_PARSE = "test_chat_sdk_parse_structured"
SDK_CAPS = {"openai_contract": True, "structured_output": True}


def test_sdk_parse_passes_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(tmp_path, SDK_PARSE, SDK_CAPS)
    assert set(results.values()) == {"pass"} and len(results) == 2, results
    assert code == 0


def test_sdk_parse_fails_when_the_schema_is_dropped(tmp_path):
    code, results = run_contract(
        tmp_path, SDK_PARSE, SDK_CAPS, openai_contract_defect="response_format_ignored"
    )
    assert set(results.values()) == {"fail"} and len(results) == 2, results
    assert code == 1


PARITY = "test_chat_streaming_matches_json"


def test_chat_stream_parity_passes_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(tmp_path, PARITY, {"chat_repeats": ["greedy"]})
    assert set(results.values()) == {"pass"} and len(results) == 1, results
    assert code == 0


def test_chat_stream_parity_skips_without_greedy(tmp_path):
    code, results = run_contract(tmp_path, PARITY, {"chat_repeats": ["seed"]})
    assert set(results.values()) == {"skip"} and len(results) == 1, results
    assert code == 0


@pytest.mark.parametrize("defect", ["stream_text_differs", "stream_prompt_count_differs"])
def test_chat_stream_parity_fails_when_the_stream_differs(tmp_path, defect):
    code, results = run_contract(
        tmp_path, PARITY, {"chat_repeats": ["greedy"]}, openai_contract_defect=defect
    )
    assert set(results.values()) == {"fail"} and len(results) == 1, results
    assert code == 1


EFFORT_ORDER = "test_chat_reasoning_effort_order"
EFFORT_CAPS = {"openai_contract": True, "chat_reasoning_efforts": ["low", "medium", "high"]}


def test_effort_order_passes_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(tmp_path, EFFORT_ORDER, EFFORT_CAPS)
    assert set(results.values()) == {"pass"} and len(results) == 1, results
    assert code == 0


def test_effort_order_skips_with_one_effort(tmp_path):
    caps = {"openai_contract": True, "chat_reasoning_efforts": ["none", "low"]}
    code, results = run_contract(tmp_path, EFFORT_ORDER, caps)
    assert set(results.values()) == {"skip"} and len(results) == 1, results
    assert code == 0


@pytest.mark.parametrize("defect", ["effort_ignored", "effort_inverted"])
def test_effort_order_defects_fail(tmp_path, defect):
    code, results = run_contract(tmp_path, EFFORT_ORDER, EFFORT_CAPS, openai_contract_defect=defect)
    assert set(results.values()) == {"fail"} and len(results) == 1, results
    assert code == 1


def test_effort_order_judges_medians_through_per_sample_noise(tmp_path):
    """+-40% noise per sample inverts single samples (medium 56 thought tokens, high 30) but not
    the medians of three, which equal the ordered means (plus the one-token answer); every
    sample is a planned request on record."""
    code, (record,) = contract_records(
        tmp_path, EFFORT_ORDER, EFFORT_CAPS, openai_contract_defect="effort_noisy"
    )
    assert record["outcome"] == "pass", record
    names = [check["name"] for check in record["checks"]]
    for effort in ("low", "medium", "high"):
        assert names.count(f"completion_tokens[{effort}]") == 3
    medians = next(c for c in record["checks"] if c["name"] == "median_completion_tokens_by_effort")
    assert medians["observed"] == {"low": 31, "medium": 41, "high": 51}
    assert code == 0


PROMPT_OPTIONS = "test_completions_token_ids or test_completions_truncated_prompt"
PROMPT_OPTION_CAPS = {
    "openai_contract": True,
    "vllm_options": ["return_token_ids", "truncate_prompt_tokens"],
}


def test_prompt_options_pass_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(
        tmp_path, PROMPT_OPTIONS, PROMPT_OPTION_CAPS, surfaces=("completions",)
    )
    assert set(results.values()) == {"pass"} and len(results) == 2, results
    assert code == 0


def test_prompt_options_skip_where_undeclared(tmp_path):
    code, results = run_contract(
        tmp_path, PROMPT_OPTIONS, {"openai_contract": True}, surfaces=("completions",)
    )
    assert set(results.values()) == {"skip"} and len(results) == 2, results
    assert code == 0


@pytest.mark.parametrize(
    "defect,failing",
    [("token_ids_short", "test_completions_token_ids"), ("truncation_ignored", "truncated")],
)
def test_prompt_option_defects_fail(tmp_path, defect, failing):
    code, results = run_contract(
        tmp_path,
        PROMPT_OPTIONS,
        PROMPT_OPTION_CAPS,
        surfaces=("completions",),
        openai_contract_defect=defect,
    )
    assert outcome(results, failing) == "fail", results
    assert list(results.values()).count("fail") == 1, results
    assert code == 1


HIDDEN = "test_chat_reasoning_hidden"
HIDDEN_CAPS = {"openai_contract": True, "vllm_options": ["include_reasoning"]}


def test_hidden_reasoning_passes_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(tmp_path, HIDDEN, HIDDEN_CAPS)
    assert set(results.values()) == {"pass"} and len(results) == 2, results
    assert code == 0


def test_hidden_reasoning_skips_where_undeclared(tmp_path):
    code, results = run_contract(tmp_path, HIDDEN, {"openai_contract": True})
    assert set(results.values()) == {"skip"} and len(results) == 2, results
    assert code == 0


@pytest.mark.parametrize("defect", ["include_reasoning_ignored", "hidden_reasoning_in_content"])
def test_hidden_reasoning_defects_fail(tmp_path, defect):
    code, results = run_contract(tmp_path, HIDDEN, HIDDEN_CAPS, openai_contract_defect=defect)
    assert set(results.values()) == {"fail"} and len(results) == 2, results
    assert code == 1


STOP_REASON = "stop_reason"
STOP_REASON_CAPS = {"openai_contract": True, "vllm_options": ["stop_reason"], **QUIET}
BOTH_SURFACES = ("chat_completions", "completions")


def test_stop_reason_passes_on_a_healthy_fleet(tmp_path):
    code, results = run_contract(tmp_path, STOP_REASON, STOP_REASON_CAPS, surfaces=BOTH_SURFACES)
    assert set(results.values()) == {"pass"} and len(results) == 2, results
    assert code == 0


def test_stop_reason_skips_where_undeclared(tmp_path):
    caps = {"openai_contract": True, **QUIET}
    code, results = run_contract(tmp_path, STOP_REASON, caps, surfaces=BOTH_SURFACES)
    assert set(results.values()) == {"skip"} and len(results) == 2, results
    assert code == 0


def test_stop_reason_fails_when_missing(tmp_path):
    code, results = run_contract(
        tmp_path,
        STOP_REASON,
        STOP_REASON_CAPS,
        surfaces=BOTH_SURFACES,
        openai_contract_defect="stop_reason_missing",
    )
    assert set(results.values()) == {"fail"} and len(results) == 2, results
    assert code == 1
