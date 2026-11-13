"""Completions cases that require output send a prompt the model continues.

With no chat template, a raw instruction gives a model no cue, and ending at once is a fair
reply (`cueless_eos`). The cases below send the surface's `reply_prompt`, an open sentence, so
they pass there, and still fail on output when every reply is empty whatever the prompt.
"""

from __future__ import annotations

import json

import pytest
import yaml

from llmdv.surfaces.base import OPEN_PROMPT
from llmdv.surfaces.completions import COMPLETIONS
from tests.evolution_helpers import failed, run_evolution
from tests.fakes import FakeFleet, run_llmdv
from tests.test_suites_against_fake import write_profile

CASES = (
    "openai_completions.basic_non_streaming",
    "openai_completions.response_fields",
    "evolution.response_contract",
    "evolution.unknown_header",
    "unhappy.token_limit_over_context",
)
SELECT = (
    "test_completions_basic or test_completions_response_fields"
    " or test_response_keeps_its_contract or test_unknown_header"
    " or test_token_limit_over_context"
)


def run(tmp_path, **fake):
    with FakeFleet(openai_contract=True, **fake) as fleet:
        path = write_profile(tmp_path, fleet, responses=False)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["completions"]
        raw["capabilities"].update(
            openai_contract=["completions"],
            contract_baseline={"completions": {"response": {"llmdv_seed": ["absent"]}}},
            forward_compat={"unknown_header": "accepted"},
        )
        path.write_text(yaml.safe_dump(raw))
        return run_evolution(path, tmp_path / "run", SELECT)


def test_completions_answers_with_an_open_sentence():
    assert COMPLETIONS.reply_prompt == OPEN_PROMPT


def test_output_cases_pass_where_a_raw_instruction_ends_at_once(tmp_path):
    code, by_case = run(tmp_path, cueless_eos=True)
    outcomes = {case: by_case[case]["completions"]["outcome"] for case in CASES}
    assert set(outcomes.values()) == {"pass"}, outcomes
    assert code == 0


@pytest.mark.parametrize("case", CASES)
def test_output_cases_fail_when_every_reply_is_empty(tmp_path, case):
    code, by_case = run(tmp_path, completions_end_at_once=True)
    record = by_case[case]["completions"]
    assert record["outcome"] == "fail", record
    assert failed(record, "output_chars"), record["checks"]
    assert code == 1


# Every other case that can run on Completions and requires output, by case id, with the
# check an empty reply fails. The transport and header cases ride on Completions when it is
# the only generation surface an ingress exposes.
SECRET = "llmdv-reply-prompt-secret"
ALIAS = "llmdv-client-alias"
MOVED = {
    "headers.credentials_ignored": "output_chars",
    "long_context.output_overflow": "output_overflow_clamp",
    "openai_completions.basic_parses_with_openai_sdk": "output_chars",
    "openai_completions.logprobs": "text_logprobs",
    "openai_completions.multiple_choices": "output_chars",
    "openai_completions.null_optional_fields": "output_chars",
    "openai_completions.stream_chunk_fields": "output_chars",
    "scenarios.client_model_names": "output_chars",
    "streaming.disconnect_before_output": "output_chars",
    "transport.accept_event_stream": "output_chars",
    "transport.chunked_request": "output_chars",
    "transport.client_headers": "output_chars",
    "transport.content_type": "output_chars",
    "transport.expect_continue": "output_chars",
    "transport.header_name_case": "output_chars",
    "transport.header_underscore": "output_chars",
    "transport.long_header": "output_chars",
    "transport.path_variant": "output_chars",
    "transport.query_string": "output_chars",
    "transport.request_gzip": "output_chars",
    "unhappy.missing_model": "output_chars",
}
MOVED_SELECT = (
    "test_unauthenticated_ingress_ignores_credentials or test_output_budget_over_the_window"
    " or test_completions_parse_with_pinned_sdk or test_completions_logprobs"
    " or test_completions_multiple_choices or test_completions_null_optional_fields"
    " or test_completions_stream_chunk_fields or test_client_model_names"
    " or test_disconnect_before_output or test_accept_does_not_choose_the_response_shape"
    " or test_chunked_request_body or test_client_and_sdk_headers or test_content_type_variants"
    " or test_expect_continue or test_header_names_are_case_insensitive"
    " or test_header_name_with_underscore or test_long_header_value or test_path_variant"
    " or test_query_string_on_an_api_path or test_gzip_request_body or test_missing_model"
)
# The authenticated request comes first; the case cannot run on an ingress without a credential.
AUTHENTICATED = {"headers.required_authentication": "output_chars"}


def run_moved(tmp_path, authenticated: bool, **fake):
    """Run the moved cases on an ingress whose only generation surface is Completions."""
    if authenticated:
        fake["auth_header"] = ("Authorization", f"Bearer {SECRET}")
    with FakeFleet(
        openai_contract=True, served_aliases=(ALIAS,), serve_missing_model=True, **fake
    ) as fleet:
        path = write_profile(tmp_path, fleet, responses=False)
        raw = yaml.safe_load(path.read_text())
        ingress = raw["ingresses"][0]
        ingress["surfaces"] = ["completions"]
        ingress["error_status"]["output_overflow"] = [200]
        if authenticated:
            ingress["auth"] = {"type": "bearer", "key_env": "LLMDV_REPLY_KEY"}
        raw["model"]["aliases"] = [ALIAS]
        raw["capabilities"].update(
            openai_contract=["completions"],
            chat_options=["n"],
            client_model_names=[ALIAS],
            scenario_checks=["output_overflow"],
        )
        raw["clients"] = [c for c in raw["clients"] if "chat_completions" not in c["surfaces"]]
        path.write_text(yaml.safe_dump(raw))
        run = tmp_path / "run"
        select = "test_required_authentication" if authenticated else MOVED_SELECT
        result = run_llmdv(
            "verify",
            "--profile",
            str(path),
            "--local-output",
            str(run),
            "--pod-logs",
            "off",
            "--skip-preflight",
            "--evidence",
            "all",
            "-k",
            select,
            env={"LLMDV_REPLY_KEY": SECRET},
        )
    records = [json.loads(line) for line in (run / "results.jsonl").read_text().splitlines()]
    by_case: dict[str, list[dict]] = {}
    for record in records:
        # Generic cases also report a skip for each surface the ingress does not expose.
        if record.get("case_id") and record.get("surface") in (None, "completions"):
            by_case.setdefault(record["case_id"], []).append(record)
    return result.returncode, by_case


@pytest.mark.parametrize("authenticated", [False, True], ids=["open", "authenticated"])
def test_moved_cases_pass_where_a_raw_instruction_ends_at_once(tmp_path, authenticated):
    code, by_case = run_moved(tmp_path, authenticated, cueless_eos=True)
    expected = AUTHENTICATED if authenticated else MOVED
    outcomes = {case: {r["outcome"] for r in by_case.get(case, [])} for case in expected}
    assert outcomes == dict.fromkeys(expected, {"pass"}), outcomes
    assert code == 0


@pytest.mark.parametrize("authenticated", [False, True], ids=["open", "authenticated"])
def test_moved_cases_fail_on_output_when_every_reply_is_empty(tmp_path, authenticated):
    code, by_case = run_moved(tmp_path, authenticated, completions_end_at_once=True)
    expected = AUTHENTICATED if authenticated else MOVED
    # One request in a case failing on output is enough: transport.path_variant also has a
    # variant the fake does not serve, which passes as a refusal.
    observed = {
        case: any(r["outcome"] == "fail" and failed(r, check) for r in by_case.get(case, []))
        for case, check in expected.items()
    }
    assert observed == dict.fromkeys(expected, True), {
        case: [
            (r["outcome"], [c["name"] for c in r["checks"] if not c["ok"]])
            for r in by_case.get(case, [])
        ]
        for case, ok in observed.items()
        if not ok
    }
    assert code == 1
