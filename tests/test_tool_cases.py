"""The tools suite against the generic tool engine: each case passes on a healthy fleet and
fails on the defect it exists to catch."""

from __future__ import annotations

import pytest
import yaml

from llmdv.suites.tools.test_streaming import CODE
from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile

ALL = ("chat_completions", "messages", "responses")


def run_case(
    tmp_path,
    test,
    defect=None,
    *,
    auto=True,
    markup=("<tool_call>",),
    errors=None,
    images=True,
    structured=False,
    strict_level=None,
    warm_cache=False,
):
    """Run one tools-suite test function on every tool surface; outcome per surface."""
    with FakeFleet(
        tool_engine=True,
        tool_engine_defect=defect,
        tool_engine_warm_cache=warm_cache,
        transport={"multimodal": True},
    ) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = list(ALL)
        raw["ingresses"][0]["error_status"].update(errors or {})
        raw["capabilities"]["vision"] = images
        if structured:
            raw["capabilities"]["structured_output"] = list(ALL)
        raw["model"]["tools"] = {
            "parser": "example-parser",
            "auto_choice": auto,
            "markup": list(markup),
        }
        if strict_level:
            raw["model"]["tools"]["strict_level"] = strict_level
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", test)
    rows = {r["surface"]: r for r in records if r.get("surface") in ALL}
    assert rows, records
    return code, {surface: row["outcome"] for surface, row in rows.items()}, rows


def outcomes(tmp_path, test, defect=None, **kw):
    return run_case(tmp_path, test, defect, **kw)[1]


def failed_check(rows, surface):
    return next(c["name"] for c in rows[surface]["checks"] if c.get("ok") is False)


# ---------------------------------------------------------------------------- tool_choice


CHAT_AND_MESSAGES = ("chat_completions", "messages")


@pytest.mark.parametrize(
    "test,defect,failing,check",
    [
        ("test_auto_call_round_trip", None, (), None),
        ("test_auto_call_round_trip", "ignores_result", ALL, "tool_result_used"),
        ("test_auto_call_round_trip", "markup_in_text", ALL, "tool_markup_in_answer"),
        ("test_auto_call_round_trip", "stop_finish", CHAT_AND_MESSAGES, "tool_turn_finish"),
        ("test_auto_call_round_trip", "bad_messages_id", ("messages",), "tool_call_id_pattern"),
        ("test_auto_without_need", None, (), None),
        ("test_auto_without_need", "auto_always_calls", ALL, "tool_calls_absent"),
        ("test_required_call", None, (), None),
        # A leak beside no call is recorded as the leak, not only as a missing call.
        ("test_required_call", "call_as_markup", ALL, "tool_markup_in_answer"),
        # A non-strict tool's arguments are not held to its schema (#270).
        ("test_required_call", "schema_violation", (), None),
        ("test_none_answers_in_text", None, (), None),
        ("test_none_answers_in_text", "none_empty", ALL, "answer_text"),
        ("test_none_answers_in_text", "none_markup", ALL, "tool_markup_in_answer"),
        ("test_named_second_tool", None, (), None),
        ("test_named_second_tool", "ignores_named", ALL, "tool_call_names"),
    ],
)
def test_choice_cases(tmp_path, test, defect, failing, check):
    code, got, rows = run_case(tmp_path, test, defect)
    assert got == {s: "fail" if s in failing else "pass" for s in ALL}, got
    assert code == (1 if failing else 0)
    for surface in failing:
        assert failed_check(rows, surface) == check


def test_auto_cases_skip_without_the_declaration(tmp_path):
    with FakeFleet(tool_engine=True) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = list(ALL)
        raw["model"]["tools"] = {"parser": "example-parser"}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_auto_without_need")
        assert not [seen for seen in fleet.seen if seen[0] == "POST"]
    assert code == 0
    assert {r["skip_class"] for r in records if r.get("surface") in ALL} == {"undeclared"}


@pytest.mark.parametrize("defect,outcome", [(None, "pass"), ("ignores_allowed", "fail")])
def test_allowed_tools_on_responses(tmp_path, defect, outcome):
    assert outcomes(tmp_path, "test_allowed_tools and not chat", defect) == {"responses": outcome}


@pytest.mark.parametrize(
    "defect,outcome",
    [
        (None, "pass"),
        ("ignores_allowed", "fail"),
        ("rejects_allowed", "pass"),
        ("allowed_5xx", "fail"),
    ],
)
def test_allowed_tools_on_chat_honored_or_refused(tmp_path, defect, outcome):
    assert outcomes(tmp_path, "test_allowed_tools_chat", defect) == {"chat_completions": outcome}


def test_markup_declaration_is_validated(tmp_path):
    from llmdv.profile.loader import ProfileError, load

    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["model"]["tools"] = {"parser": "p", "markup": [""]}
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ProfileError, match="empty string"):
        load(path)
    raw["model"]["tools"] = {"parser": "p", "markup": ["<call>"]}
    path.write_text(yaml.safe_dump(raw))
    assert load(path).model.tools.markup == ("<call>",)


# ---------------------------------------------------------------------------- parallel calls


@pytest.mark.parametrize(
    "test,defect,failing,check",
    [
        ("test_parallel_calls", None, (), None),
        ("test_parallel_calls", "merges_parallel", ALL, "tool_call_names"),
        ("test_parallel_calls", "duplicate_ids", ALL, "tool_call_ids"),
        ("test_parallel_calls and required", "merges_parallel", ALL, "tool_call_names"),
        ("test_three_parallel_calls", None, (), None),
        ("test_three_parallel_calls", "drops_third_call", ALL, "tool_call_names"),
        ("test_parallel_disabled", None, (), None),
        ("test_parallel_disabled", "ignores_parallel_off", ALL, "tool_call_count"),
        ("test_parallel_stream", None, (), None),
        ("test_parallel_stream", "merged_stream", ALL, None),
        ("test_parallel_stream and required", "merged_stream", ALL, None),
        ("test_parallel_stream", "opener_without_name", ("chat_completions",), "chat_tool_openers"),
    ],
)
def test_parallel_cases(tmp_path, test, defect, failing, check):
    code, got, rows = run_case(tmp_path, test, defect)
    assert got == {s: "fail" if s in failing else "pass" for s in ALL}, got
    assert code == (1 if failing else 0)
    for surface in failing if check else ():
        assert failed_check(rows, surface) == check


# ---------------------------------------------------------------------------- streaming


@pytest.mark.parametrize(
    "test,defect,failing,check",
    [
        ("test_auto_stream", None, (), None),
        ("test_auto_stream", "opener_without_name", ("chat_completions",), "chat_tool_openers"),
        ("test_auto_stream", "schema_violation", (), None),
        ("test_stream_matches_json", None, (), None),
        ("test_stream_matches_json", "stream_args_differ", ALL, "streamed_equals_json"),
    ],
)
def test_streaming_cases(tmp_path, test, defect, failing, check):
    code, got, rows = run_case(tmp_path, test, defect)
    assert got == {s: "fail" if s in failing else "pass" for s in ALL}, got
    assert code == (1 if failing else 0)
    for surface in failing:
        assert failed_check(rows, surface) == check


# ---------------------------------------------------------------------------- results


@pytest.mark.parametrize(
    "test,defect,failing,check",
    [
        ("test_result_parts", None, (), None),
        ("test_result_parts", "drops_parts", ALL, "tool_result_used"),
        ("test_result_parts", "markup_in_text", ALL, "tool_markup_in_answer"),
        ("test_result_large", None, (), None),
        ("test_result_large", "truncates_results", ALL, "tool_result_used"),
        ("test_result_unicode", None, (), None),
        ("test_result_unicode", "escapes_unicode", ALL, "tool_result_used"),
        ("test_result_empty", None, (), None),
        ("test_result_empty", "empty_result_5xx", ALL, "http_status"),
        ("test_result_multiple", None, (), None),
        ("test_result_multiple", "drops_second_result", ALL, "tool_result_used"),
        ("test_result_unknown_id", None, (), None),
        ("test_result_unknown_id", "accepts_orphans", ALL, "orphan_tool_result_status"),
        ("test_result_missing_id", None, (), None),
        ("test_result_missing_id", "accepts_orphans", ALL, "orphan_tool_result_status"),
    ],
)
def test_result_cases(tmp_path, test, defect, failing, check):
    code, got, rows = run_case(tmp_path, test, defect)
    assert got == {s: "fail" if s in failing else "pass" for s in ALL}, got
    assert code == (1 if failing else 0)
    for surface in failing:
        assert failed_check(rows, surface) == check


@pytest.mark.parametrize("defect,outcome", [("accepts_orphans", "pass"), (None, "fail")])
def test_orphan_results_follow_the_declaration(tmp_path, defect, outcome):
    """A fleet declared to serve orphan results passes when it serves them, fails when not."""
    got = outcomes(tmp_path, "test_result_unknown_id", defect, errors={"orphan_tool_result": [200]})
    assert got == {s: outcome for s in ALL}


@pytest.mark.parametrize(
    "test,defect,expected",
    [
        ("test_result_is_error", None, {"messages": "pass"}),
        ("test_result_is_error", "drops_is_error", {"messages": "fail"}),
        ("test_result_image and not responses", None, {"messages": "pass"}),
        ("test_result_image and not responses", "image_as_text", {"messages": "fail"}),
        ("test_result_image_responses", None, {"responses": "pass"}),
        ("test_result_image_responses", "image_as_text", {"responses": "fail"}),
        ("test_result_image_read and not responses", None, {"messages": "pass"}),
        ("test_result_image_read and not responses", "drops_result_image", {"messages": "fail"}),
        ("test_result_image_read and not responses", "image_as_text", {"messages": "fail"}),
        ("test_result_image_read_responses", None, {"responses": "pass"}),
        ("test_result_image_read_responses", "drops_result_image", {"responses": "fail"}),
    ],
)
def test_single_surface_result_cases(tmp_path, test, defect, expected):
    assert outcomes(tmp_path, test, defect) == expected


IMAGE_ONLY = "test_result_image and not responses and not read"


@pytest.mark.parametrize(
    "test,defect,expected,check",
    [
        ("test_result_is_error", None, "pass", None),
        ("test_result_is_error", "drops_is_error", "fail", "is_error_input_tokens"),
        (IMAGE_ONLY, None, "pass", None),
        (IMAGE_ONLY, "image_as_text", "fail", "image_input_tokens"),
    ],
)
def test_result_prompt_sizes_count_cached_tokens(tmp_path, test, defect, expected, check):
    """Messages input_tokens is only the uncached tail; the cases compare the whole prompt.

    Read alone, a warm cache's tail is equal with and without is_error, and an image inlined
    as text hides under the ceiling (#333).
    """
    _, got, rows = run_case(tmp_path, test, defect, warm_cache=True)
    assert got == {"messages": expected}
    if check:
        assert failed_check(rows, "messages") == check


def test_a_dropped_result_image_fails_on_what_the_answer_named(tmp_path):
    _, got, rows = run_case(tmp_path, "test_result_image_read_responses", "drops_result_image")
    assert got == {"responses": "fail"}
    assert failed_check(rows, "responses") == "image_content_read"


def test_reading_a_result_image_is_skipped_where_images_are_undeclared(tmp_path):
    _, got, rows = run_case(tmp_path, "test_result_image_read_responses", images=False)
    assert got == {"responses": "skip"}
    assert rows["responses"]["skip_class"] == "undeclared"


# ---------------------------------------------------------------------------- definitions


@pytest.mark.parametrize(
    "test,defect,failing,check",
    [
        ("test_schema_features", None, (), None),
        ("test_schema_features and nested", "schema_violation", ALL, "tool_call_arguments_schema"),
        ("test_argument_types", None, (), None),
        ("test_argument_types and sse", "args_as_strings", ALL, "tool_call_arguments_schema"),
        ("test_no_parameters", None, (), None),
        ("test_no_parameters and minimal", "empty_arguments", ALL, "tool_call_arguments_json"),
        ("test_strict", None, (), None),
        ("test_strict", "rejects_strict", ALL, "http_status"),
        # A strict tool's schema is a promise, so a violation is a defect.
        ("test_strict", "schema_violation", ALL, "tool_call_arguments_schema"),
        ("test_many_tools", None, (), None),
        ("test_many_tools", "truncates_tools", ALL, "tool_call_names"),
        ("test_name_charset", None, (), None),
        ("test_name_charset", "truncates_names", ALL, "tool_call_names"),
        ("test_name_outside_spec", None, (), None),
        ("test_name_outside_spec", "renames_dotted", ALL, "tool_call_names"),
        ("test_long_description", None, (), None),
        ("test_long_description", "truncates_description", ALL, "description_tail_used"),
        ("test_duplicate_names", None, (), None),
        ("test_duplicate_names", "duplicate_5xx", ALL, "refusal_status"),
        (
            "test_schema_features and keyword_names",
            "strips_strict_keys",
            ALL,
            "tool_call_arguments_schema",
        ),
        (
            "test_schema_features and (generated or string_formats or draft07)",
            "rejects_schema_keywords",
            ALL,
            "http_status",
        ),
        ("test_big_int_argument", None, (), None),
        ("test_big_int_argument", "big_int_rounded", ALL, "big_int_argument"),
    ],
)
def test_definition_cases(tmp_path, test, defect, failing, check):
    code, records = None, None
    with FakeFleet(tool_engine=True, tool_engine_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = list(ALL)
        raw["model"]["tools"] = {"parser": "example-parser", "auto_choice": True}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", test)
    rows = [r for r in records if r.get("surface") in ALL]
    assert rows, records
    for row in rows:
        expected = "fail" if row["surface"] in failing else "pass"
        assert row["outcome"] == expected, (row["test"] if "test" in row else row, expected)
        if expected == "fail":
            bad = next(c["name"] for c in row["checks"] if c.get("ok") is False)
            assert bad == check, (row["surface"], bad)
    assert code == (1 if failing else 0)


# ---------------------------------------------------------------------------- tool types


@pytest.mark.parametrize(
    "test,defect,expected",
    [
        ("test_server_tool", None, {"messages": "pass"}),
        ("test_server_tool", "tool_type_5xx", {"messages": "fail"}),
        ("test_custom_tool", None, {s: "pass" for s in ALL}),
        (
            "test_custom_tool",
            "tool_type_5xx",
            {"chat_completions": "fail", "messages": "pass", "responses": "fail"},
        ),
        ("test_namespace", None, {"responses": "pass"}),
        ("test_namespace", "tool_type_5xx", {"responses": "fail"}),
        ("test_input_examples", None, {"messages": "pass"}),
        ("test_input_examples", "input_examples_5xx", {"messages": "fail"}),
        ("test_eager_input_streaming", None, {"messages": "pass"}),
        ("test_eager_input_streaming", "schema_violation", {"messages": "pass"}),
    ],
)
def test_tool_type_cases(tmp_path, test, defect, expected):
    code, records = None, None
    with FakeFleet(tool_engine=True, tool_engine_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = list(ALL)
        raw["model"]["tools"] = {"parser": "example-parser"}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", test)
    got: dict[str, set] = {}
    for row in records:
        if row.get("surface") in ALL:
            got.setdefault(row["surface"], set()).add(row["outcome"])
    assert {s: "fail" if "fail" in o else "pass" for s, o in got.items()} == expected, got
    assert code == (1 if "fail" in expected.values() else 0)


# ---------------------------------------------------------------------------- round two


@pytest.mark.parametrize(
    "test,defect,expected",
    [
        ("test_parallel_disabled and required", None, {s: "pass" for s in ALL}),
        ("test_parallel_disabled and required", "ignores_parallel_off", {s: "fail" for s in ALL}),
        ("test_allowed_tools and auto", None, {"responses": "pass"}),
        ("test_allowed_tools and auto", "ignores_allowed", {"responses": "fail"}),
        ("test_max_tool_calls", "duplicate_ids", {"responses": "fail"}),
        ("test_truncated_call", None, {s: "pass" for s in ALL}),
        ("test_truncated_call", "truncated_as_call", {s: "fail" for s in ALL}),
        ("test_result_then_text", None, {s: "pass" for s in ALL}),
        ("test_result_then_text", "drops_trailing_text", {s: "fail" for s in ALL}),
        ("test_history_with_text", None, {s: "pass" for s in ALL}),
        ("test_history_with_text", "rejects_text_beside_calls", {s: "fail" for s in ALL}),
        ("test_result_blocks", None, {"messages": "pass"}),
        ("test_result_blocks", "drops_result_blocks", {"messages": "fail"}),
    ],
)
def test_limit_and_history_cases(tmp_path, test, defect, expected):
    assert outcomes(tmp_path, test, defect) == expected


def test_max_tool_calls_allows_multiple_function_calls(tmp_path):
    code, got, rows = run_case(tmp_path, "test_max_tool_calls")
    assert code == 0
    assert got == {"responses": "pass"}
    calls = next(c for c in rows["responses"]["checks"] if c["name"] == "tool_calls")
    assert calls["ok"] is True
    assert [call["name"] for call in calls["observed"]] == ["get_weather", "get_time"]


def _variant(row) -> str:
    surface = row["surface"]
    variant = row.get("variant") or surface
    return variant if variant.startswith(surface) else f"{surface}/{variant}"


def by_variant(tmp_path, test, defect=None, *, chat_options=(), structured=False):
    """(outcome, first failing check) per `surface/parameter`."""
    with FakeFleet(tool_engine=True, tool_engine_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = list(ALL)
        raw["model"]["tools"] = {"parser": "example-parser", "auto_choice": True}
        raw["capabilities"]["chat_options"] = list(chat_options)
        raw["capabilities"]["structured_output"] = list(ALL) if structured else False
        path.write_text(yaml.safe_dump(raw))
        _, records = run_suites(path, tmp_path / "run", "-k", test)
    rows = [r for r in records if r.get("surface") in ALL]
    assert rows, records
    return {
        _variant(row): (
            row["outcome"],
            next((c["name"] for c in row["checks"] if c.get("ok") is False), None),
        )
        for row in rows
    }


@pytest.mark.parametrize(
    "test,defect,failing",
    [
        ("test_named_stream", None, {}),
        (
            "test_named_stream",
            "forced_stream_as_text",
            {s: "tool_call_count" for s in ALL},
        ),
        ("test_code_arguments", None, {}),
        # One newline ending the last line is a fair copy, as two models write it.
        ("test_code_arguments", "ends_code_with_newline", {}),
        (
            "test_code_arguments",
            "ends_code_with_blank_line",
            {f"{s}/{m}": "code_content_exact" for s in ALL for m in ("json", "sse")},
        ),
        (
            "test_code_arguments",
            "doubles_backslashes",
            {f"{s}/{m}": "code_content_exact" for s in ALL for m in ("json", "sse")},
        ),
        (
            "test_code_arguments",
            "reorders_code_lines",
            {f"{s}/{m}": "code_content_exact" for s in ALL for m in ("json", "sse")},
        ),
        (
            "test_code_arguments",
            "cuts_long_stream_arguments",
            {f"{s}/sse": "tool_call_arguments_json" for s in ALL},
        ),
    ],
)
def test_stream_escape_cases(tmp_path, test, defect, failing):
    got = by_variant(tmp_path, test, defect)
    expected = {
        variant: ("fail", failing[variant]) if variant in failing else ("pass", None)
        for variant in got
    }
    assert got == expected
    assert set(failing) <= set(got)


def test_code_arguments_pass_records_the_newline_allowance(tmp_path):
    """A pass on code ending in a newline records the expectation it met, not the bare code."""
    with FakeFleet(tool_engine=True, tool_engine_defect="ends_code_with_newline") as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = list(ALL)
        raw["model"]["tools"] = {"parser": "example-parser", "auto_choice": True}
        path.write_text(yaml.safe_dump(raw))
        _, records = run_suites(path, tmp_path / "run", "-k", "test_code_arguments")
    rows = [r for r in records if r.get("surface") in ALL]
    assert rows, records
    for row in rows:
        assert row["outcome"] == "pass", row
        check = next(c for c in row["checks"] if c["name"] == "code_content_exact")
        assert check["ok"] is True
        assert check["observed"] == CODE + "\n"
        assert check["expected"] == f"{CODE}, optionally ending with one newline"


@pytest.mark.parametrize(
    "test,defect,failing",
    [
        ("test_chained_calls", None, {}),
        ("test_chained_calls", "ignores_result", {s: "chained_argument" for s in ALL}),
        ("test_chained_calls", "no_call_after_result", {s: "tool_call_count" for s in ALL}),
        ("test_chained_calls", "index_ids", {s: "tool_call_ids_across_turns" for s in ALL}),
        (
            "test_parallel_stream",
            "index_ids",
            {
                f"{s}/{m}": "tool_call_ids_across_responses"
                for s in ALL
                for m in ("auto", "required")
            },
        ),
        ("test_history_without_tools", None, {}),
        (
            "test_history_without_tools",
            "history_without_tools_5xx",
            {s: "refusal_status" for s in ALL},
        ),
        ("test_history_content_forms", None, {}),
        (
            "test_history_content_forms",
            "omitted_content_5xx",
            {"chat_completions/omitted": "http_status"},
        ),
    ],
)
def test_history_cases(tmp_path, test, defect, failing):
    got = by_variant(tmp_path, test, defect)
    expected = {
        variant: ("fail", failing[variant]) if variant in failing else ("pass", None)
        for variant in got
    }
    assert got == expected
    assert set(failing) <= set(got)


@pytest.mark.parametrize(
    "test,defect,failing",
    [
        ("test_history_bad_arguments", None, {}),
        (
            "test_history_bad_arguments",
            "bad_arguments_5xx",
            "refusal_status",
        ),
    ],
)
def test_bad_argument_cases(tmp_path, test, defect, failing):
    variants = {
        f"{s}/{k}" for s in ("chat_completions", "responses") for k in ("empty", "malformed")
    }
    got = by_variant(tmp_path, test, defect)
    assert set(got) == variants
    for variant in variants:
        assert got[variant] == (("fail", failing) if failing else ("pass", None)), variant


@pytest.mark.parametrize(
    "defect,failing",
    [(None, {}), ("n_second_as_text", {"chat_completions/sse": "tool_call_count"})],
)
def test_n_choices_case(tmp_path, defect, failing):
    got = by_variant(tmp_path, "test_n_choices", defect, chat_options=("n",))
    assert set(got) == {"chat_completions/json", "chat_completions/sse"}
    for variant, row in got.items():
        assert row == (("fail", failing[variant]) if variant in failing else ("pass", None))


def test_n_choices_skip_without_the_declaration(tmp_path):
    got = by_variant(tmp_path, "test_n_choices")
    assert {row[0] for row in got.values()} == {"skip"}, got


@pytest.mark.parametrize(
    "defect,expected",
    [
        (None, ("pass", None)),
        ("unsigned_thinking", ("fail", "messages_thinking")),
        ("rejects_thinking_replay", ("fail", "http_status")),
    ],
)
def test_thinking_round_trip_case(tmp_path, defect, expected):
    with FakeFleet(tool_engine=True, tool_engine_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = list(ALL)
        raw["model"]["tools"] = {"parser": "example-parser", "auto_choice": True}
        raw["capabilities"]["messages_thinking"] = ["enabled"]
        path.write_text(yaml.safe_dump(raw))
        _, records = run_suites(path, tmp_path / "run", "-k", "test_thinking_round_trip")
    [row] = [r for r in records if r.get("surface") == "messages"]
    failed = next((c["name"] for c in row["checks"] if c.get("ok") is False), None)
    assert (row["outcome"], failed) == expected


def test_thinking_round_trip_skips_without_the_declaration(tmp_path):
    got = by_variant(tmp_path, "test_thinking_round_trip")
    assert got == {"messages": ("skip", None)}, got


@pytest.mark.parametrize(
    "defect,failing",
    [
        (None, set()),
        (
            "named_no_parameters_5xx",
            {f"{s}/{v}/named" for s in ALL for v in ("minimal", "empty_properties")},
        ),
        (
            "empty_arguments",
            {
                f"{s}/{v}/{c}"
                for s in ALL
                for v in ("minimal", "empty_properties")
                for c in ("required", "named", "auto")
            },
        ),
    ],
)
def test_no_parameter_choice_cases(tmp_path, defect, failing):
    got = by_variant(tmp_path, "test_no_parameters", defect)
    assert len(got) == 18, sorted(got)
    assert {v for v, (outcome, _) in got.items() if outcome == "fail"} == failing, got


@pytest.mark.parametrize(
    "defect,failing",
    [
        (None, {}),
        ("format_blocks_calls", {s: "tool_call_count" for s in ALL}),
        ("format_dropped_after_result", {s: "structured_output_schema" for s in ALL}),
    ],
)
def test_output_format_cases(tmp_path, defect, failing):
    got = by_variant(tmp_path, "test_with_output_format", defect, structured=True)
    assert set(got) == set(ALL), got
    for surface, row in got.items():
        assert row == (("fail", failing[surface]) if failing else ("pass", None)), surface


def test_output_format_case_skips_without_the_declaration(tmp_path):
    got = by_variant(tmp_path, "test_with_output_format")
    assert {row for row in got.values()} == {("skip", None)}, got


@pytest.mark.parametrize(
    "test,defect,failing",
    [
        ("test_no_description", None, {}),
        ("test_no_description", "description_required_5xx", {s: "http_status" for s in ALL}),
        ("test_result_partial", None, {}),
        ("test_result_partial", "missing_result_5xx", {s: "refusal_status" for s in ALL}),
    ],
)
def test_optional_parts_cases(tmp_path, test, defect, failing):
    got = by_variant(tmp_path, test, defect)
    assert set(got) == set(ALL), got
    for surface, row in got.items():
        assert row == (("fail", failing[surface]) if failing else ("pass", None)), surface


@pytest.mark.parametrize(
    "defect,expected",
    [(None, ("pass", None)), ("ignores_legacy_functions", ("fail", "legacy_function_call"))],
)
def test_legacy_functions_case(tmp_path, defect, expected):
    assert by_variant(tmp_path, "test_legacy_functions", defect) == {"chat_completions": expected}


@pytest.mark.parametrize(
    "defect,expected",
    [(None, ("pass", None)), ("count_ignores_tools", ("fail", "count_tokens_grow"))],
)
def test_count_tokens_case(tmp_path, defect, expected):
    assert by_variant(tmp_path, "test_token_count_with_tools", defect) == {"messages": expected}


# --------------------------------------------------------- a result that quotes the call markup


@pytest.mark.parametrize(
    "defect,outcome,check",
    [
        (None, "pass", None),
        ("markup_grammar_5xx", "fail", "http_status"),
        ("markup_empty_answer", "fail", "output_chars"),
    ],
)
def test_result_quoting_markup_then_schema(tmp_path, defect, outcome, check):
    code, got, rows = run_case(
        tmp_path, "test_result_quotes_markup_then_schema", defect, structured=True
    )
    assert got == dict.fromkeys(ALL, outcome), got
    if check:
        assert all(failed_check(rows, surface) == check for surface in ALL), rows
    else:
        # The answer quotes the tag as asked: recorded, not judged as a leak.
        for surface in ALL:
            quoted = next(
                c for c in rows[surface]["checks"] if c["name"] == "tool_markup_in_answer"
            )
            assert quoted["ok"] and quoted["observed"] == ["<tool_call>"], quoted


@pytest.mark.parametrize("quoted", [False, True])
def test_call_markup_in_any_answer_is_a_leak_unless_the_case_asks_for_it(quoted):
    from llmdv.assertions.tool_calls import assert_no_call_markup
    from llmdv.evidence import journal
    from llmdv.profile.schema import Model, Tools
    from llmdv.surfaces.base import Completion

    completion = Completion(role="assistant", text="<tool_call>get_weather", finish_reason="stop")
    active = journal.activate("call-markup")
    try:
        if quoted:
            assert_no_call_markup(
                completion, Model("m", tools=Tools(markup=("<tool_call>",))), quoted=True
            )
        else:
            with pytest.raises(AssertionError, match="tool_markup_in_answer"):
                assert_no_call_markup(completion, Model("m", tools=Tools(markup=("<tool_call>",))))
        assert [(c.name, c.ok) for c in active.checks] == [("tool_markup_in_answer", quoted)]
        assert_no_call_markup(completion, Model("m"))
        assert len(active.checks) == 1
    finally:
        journal.deactivate()


def test_result_quoting_markup_skips_without_declared_markup(tmp_path):
    _, got, rows = run_case(
        tmp_path, "test_result_quotes_markup_then_schema", markup=(), structured=True
    )
    assert got == dict.fromkeys(ALL, "skip"), got
    assert all("[undeclared]" in rows[surface]["reason"] for surface in ALL), rows


# ------------------------------------------------------ argument schemas, strict or declared


def test_a_non_strict_tool_records_a_schema_mismatch_without_failing(tmp_path):
    _, got, rows = run_case(tmp_path, "test_required_call", "schema_violation")
    assert got == dict.fromkeys(ALL, "pass"), got
    for surface in ALL:
        names = [c["name"] for c in rows[surface]["checks"]]
        assert "tool_call_arguments_unconstrained" in names, names
        assert "tool_call_arguments_schema" not in names, names


def test_a_parameter_strict_level_holds_every_tool_to_its_schema(tmp_path):
    _, got, rows = run_case(
        tmp_path, "test_required_call", "schema_violation", strict_level="parameter"
    )
    assert got == dict.fromkeys(ALL, "fail"), got
    assert all(failed_check(rows, s) == "tool_call_arguments_schema" for s in ALL), rows
