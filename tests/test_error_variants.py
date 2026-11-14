"""Error-path variant cases against a fake that validates requests, then against defects."""

from __future__ import annotations

import pytest

from llmdv import surfaces
from llmdv.surfaces import error_variants
from tests.fakes import FakeFleet
from tests.transport_helpers import exchanges, outcomes, run

VARIANT_CASES = (
    "test_empty_input or test_invalid_role or test_missing_model "
    "or test_tool_choice_without_tools or test_token_limit_over_context "
    "or test_missing_token_limit or test_invalid_image or test_image_to_a_text_only_model "
    "or test_invalid_thinking_type or test_schemaless_server_tool or test_sampling_option "
    "or test_error_envelope_fields"
)
CASES = {
    "unhappy.empty_input",
    "unhappy.invalid_role",
    "unhappy.missing_model",
    "unhappy.tool_choice_without_tools",
    "unhappy.token_limit_over_context",
    "unhappy.missing_token_limit",
    "unhappy.invalid_image",
    "unhappy.image_input",
    "unhappy.invalid_thinking_type",
    "unhappy.server_tool",
    "unhappy.sampling_option",
    "unhappy.error_envelope_fields",
}


def test_variants_pass_against_a_validating_fleet(tmp_path):
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, VARIANT_CASES, error_status={"image_input": [400]})
    assert set(by_case) == CASES
    for case in CASES:
        assert outcomes(by_case, case) <= {"pass", "skip"}, by_case[case]
        assert "pass" in outcomes(by_case, case), by_case[case]
    skipped = {r["skip_class"] for records in by_case.values() for r in records if r["skip_class"]}
    assert skipped <= {"not_implemented"}
    assert code == 0
    sent = [e["request_body"] for e in exchanges(tmp_path) if isinstance(e["request_body"], dict)]
    for forbidden in ("vllm_xargs", "kv_transfer_params", "priority", "kv_cache_report_mode"):
        assert not any(forbidden in body for body in sent)


def test_image_input_skips_unless_declared(tmp_path):
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, "test_image_to_a_text_only_model")
    assert {r["skip_class"] for r in by_case["unhappy.image_input"]} == {"undeclared"}
    assert code == 0


@pytest.mark.parametrize(
    "defect,select,case_id",
    [
        ("accepts_empty_input", "test_empty_input", "unhappy.empty_input"),
        ("accepts_unknown_role", "test_invalid_role", "unhappy.invalid_role"),
        ("variant_500", "test_missing_model", "unhappy.missing_model"),
        ("variant_500", "test_tool_choice_without_tools", "unhappy.tool_choice_without_tools"),
        ("variant_500", "test_token_limit_over_context", "unhappy.token_limit_over_context"),
        ("variant_500", "test_missing_token_limit", "unhappy.missing_token_limit"),
        ("variant_500", "test_invalid_image", "unhappy.invalid_image"),
        ("image_inlined", "test_image_to_a_text_only_model", "unhappy.image_input"),
        ("thinking_type_ignored", "test_invalid_thinking_type", "unhappy.invalid_thinking_type"),
        ("variant_500", "test_schemaless_server_tool", "unhappy.server_tool"),
        ("variant_500", "test_sampling_option", "unhappy.sampling_option"),
        ("anthropic_error_type", "test_error_envelope_fields", "unhappy.error_envelope_fields"),
        ("openai_error_untyped", "test_error_envelope_fields", "unhappy.error_envelope_fields"),
    ],
)
def test_each_variant_defect_fails_its_case(tmp_path, defect, select, case_id):
    with FakeFleet(transport={"defects": {defect}}) as fleet:
        code, by_case = run(tmp_path, fleet, select, error_status={"image_input": [400]})
    assert "fail" in outcomes(by_case, case_id), by_case.get(case_id)
    assert code == 1


def test_a_multimodal_fleet_fails_a_text_only_declaration(tmp_path):
    with FakeFleet(transport={"multimodal": True}) as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_image_to_a_text_only_model",
            error_status={"image_input": [400]},
        )
    assert "fail" in outcomes(by_case, "unhappy.image_input")
    assert code == 1


@pytest.mark.parametrize("name", sorted(surfaces.implemented()))
def test_variant_bodies_keep_the_surface_shape(name):
    surface = surfaces.get(name)
    if not hasattr(surface, "minimal_body"):
        pytest.skip("not a generation surface")
    for variant in error_variants.VARIANTS:
        body = error_variants.body(surface, "m", variant, 8)
        if body is None:
            continue
        expected = set(surface.minimal_body("m", max_tokens=8))
        assert set(body) - expected <= {"tool_choice", "tools", "thinking", "min_p", "logit_bias"}
