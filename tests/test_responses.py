"""Responses must use native input, output, status and usage instead of chat's shape."""

from __future__ import annotations

import json

import pytest
import yaml

from llmdv.assertions.responses import assert_response_contract
from llmdv.evidence import journal
from llmdv.http import redact
from llmdv.surfaces import get
from tests.fakes import FakeFleet, response_body, run_llmdv
from tests.test_suites_against_fake import run_suites, write_profile

RESPONSES = get("responses")
CASE = "openai_responses.basic_non_streaming"


def test_typed_output_items_preserve_text_reasoning_and_native_usage():
    body = response_body()
    body["output"].insert(
        0,
        {
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "working"}],
            "summary": [{"type": "summary_text", "text": "do not count this twice"}],
        },
    )
    body["output"].extend(
        [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": " more"}]},
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "!"},
                    {"type": "refusal", "refusal": "not output text"},
                    {"type": "output_text", "text": 123},
                    None,
                ],
            },
            {"type": "future_item", "text": "not output text"},
            None,
        ]
    )
    completion = RESPONSES.parse_completion(body)
    assert completion.text == "ok!"
    assert completion.reasoning_text == "working more"
    assert completion.role == "assistant"
    assert completion.finish_reason == "stop"
    assert completion.usage.prompt_tokens == 3
    assert completion.usage.completion_tokens == 2
    assert completion.usage.total_tokens == 5
    assert completion.raw == body


@pytest.mark.parametrize(
    ("text", "answer", "reasoning"),
    [("<think>working</think>ok", "ok", "working"), ("<think>working", "", "working")],
)
def test_inline_reasoning_is_split_after_joining_text_parts(text, answer, reasoning):
    body = response_body()
    body["output"][0]["content"] = [
        {"type": "output_text", "text": text[:4]},
        {"type": "output_text", "text": text[4:]},
    ]
    completion = RESPONSES.parse_completion(body)
    assert (completion.text, completion.reasoning_text) == (answer, reasoning)


@pytest.mark.parametrize(
    ("status", "details", "finish"),
    [
        ("completed", None, "stop"),
        ("incomplete", {"reason": "max_output_tokens"}, "length"),
        ("incomplete", {"reason": "content_filter"}, "content_filter"),
        ("incomplete", {"reason": "unknown"}, "status: incomplete"),
        ("incomplete", None, "status: incomplete"),
        ("completed", {"reason": "max_output_tokens"}, "status: completed"),
        ("failed", None, "status: failed"),
        ("cancelled", None, "cancelled"),
        ("in_progress", None, "status: in_progress"),
        ("queued", None, "status: queued"),
        ("stop", None, "status: stop"),  # a made-up native status cannot become a healthy finish
    ],
)
def test_native_terminal_states_are_normalized(status, details, finish):
    body = {**response_body(), "status": status, "incomplete_details": details}
    assert RESPONSES.parse_completion(body).finish_reason == finish


def test_malformed_data_normalizes_without_inventing_output_or_usage():
    for value in (None, "malformed", {}, [None, {"type": "message", "content": 123}]):
        completion = RESPONSES.parse_completion({"output": value, "usage": value})
        assert not completion.has_output
        assert completion.usage.prompt_tokens is None
        assert completion.usage.completion_tokens is None
        assert completion.usage.total_tokens is None
    body = {**response_body(), "error": {"code": "server_error", "message": "aborted"}}
    assert RESPONSES.parse_completion(body).finish_reason == "error: server_error"


@pytest.mark.parametrize("incomplete_reason", [None, "max_output_tokens", "content_filter"])
def test_smoke_runs_per_ingress_with_native_evidence(tmp_path, incomplete_reason):
    overrides = (
        {"status": "incomplete", "incomplete_details": {"reason": incomplete_reason}}
        if incomplete_reason
        else {}
    )
    with FakeFleet(response_overrides=overrides) as direct, FakeFleet() as router:
        profile = write_profile(tmp_path, direct)
        config = yaml.safe_load(profile.read_text())
        config["ingresses"].append(
            {**config["ingresses"][0], "id": "router", "base_url": router.base_url}
        )
        profile.write_text(yaml.safe_dump(config))
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_responses_basic")

    assert code == 0, records
    assert {(r["case_id"], r["ingress"]) for r in records} == {
        (CASE, ingress) for ingress in ("fake", "router")
    }
    for record in records:
        assert record["outcome"] == "pass"
        assert record["http_status"] == 200
        checks = {c["name"]: c for c in record["checks"]}
        assert all(
            checks[k]["ok"]
            for k in (
                "response_contract",
                "object",
                "model_echo",
                "output_chars",
                "usage",
                "finish_reason_recognised",
            )
        )
        assert checks["response_contract"]["observed"] == {
            "output_items": 1,
            "messages": 1,
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }
    exchanges = [
        json.loads(line) for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    assert len(exchanges) == 2
    for exchange in exchanges:
        assert exchange["url"].endswith("/v1/responses")
        body = exchange["request_body"]
        assert isinstance(body["input"], str)
        assert body["max_output_tokens"] == 256
        assert body["temperature"] == 0 and body["store"] is False
        assert "messages" not in body and "max_tokens" not in body
        assert not body.get("stream")


@pytest.mark.parametrize(
    ("overrides", "failed_check"),
    [
        ({"object": "chat.completion"}, "object"),
        ({"model": "another/model"}, "model_echo"),
        *[
            ({"status": status}, "finish_reason_recognised")
            for status in ("failed", "in_progress", "queued", None, "stop")
        ],
        ({"status": "cancelled"}, "finish_reason"),
        ({"error": {"code": "server_error", "message": "aborted"}}, "finish_reason_recognised"),
        ({"incomplete_details": {"reason": "max_output_tokens"}}, "finish_reason_recognised"),
        ({"status": "incomplete"}, "finish_reason_recognised"),
        (
            {"status": "incomplete", "incomplete_details": {"reason": "unknown"}},
            "finish_reason_recognised",
        ),
        ({"output": "malformed"}, "response_output"),
        ({"output": []}, "output_chars"),
        (
            {
                "output": [
                    None,
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": 42}],
                    },
                ]
            },
            "output_chars",
        ),
        ({"output": [{**response_body()["output"][0], "role": "user"}]}, "response_message_role"),
        (
            {"output": [{**response_body()["output"][0], "status": "in_progress"}]},
            "response_message_status",
        ),
        ({"usage": None}, "response_usage_counts"),
        ({"usage": {"input_tokens": 3, "output_tokens": 2}}, "response_usage_counts"),
        *[
            (
                {"usage": {"input_tokens": 3, "output_tokens": value, "total_tokens": 5}},
                "response_usage_counts",
            )
            for value in ("2", True, -2)
        ],
        (
            {"usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 99}},
            "usage",
        ),
    ],
)
def test_malformed_http_200_fails_with_a_recorded_check(tmp_path, overrides, failed_check):
    with FakeFleet(response_overrides=overrides) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_responses_basic")
    assert code == 1, records
    (record,) = records
    assert record["outcome"] == "fail", record
    assert record["http_status"] == 200
    assert any(c["name"] == failed_check and not c["ok"] for c in record["checks"])
    if "status" in overrides and overrides["status"] != "cancelled":
        assert f"status: {overrides['status']}" in record["reason"]
    if "error" in overrides:
        assert "error: server_error" in record["reason"]


def test_reasoning_only_budget_exhaustion_counts_as_generation(tmp_path):
    with FakeFleet(
        response_overrides={
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "working"}]}
            ],
        }
    ) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_responses_basic")
    assert code == 0, records
    (record,) = records
    assert record["outcome"] == "pass"
    assert any(c["name"] == "finish_reason" and c["observed"] == "length" for c in record["checks"])


def test_non_json_http_200_is_a_failure_not_a_runner_error(tmp_path):
    with FakeFleet(non_json_success=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_responses_basic")
    assert code == 1
    (record,) = records
    assert record["outcome"] == "fail"
    assert "JSON response object" in record["reason"]


def test_undeclared_surface_skips_without_sending_a_request(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet, responses=False)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_responses_basic")
        assert not fleet.seen
    assert code == 0
    (record,) = records
    assert (record["case_id"], record["outcome"], record["skip_class"]) == (
        CASE,
        "skip",
        "undeclared",
    )


@pytest.mark.parametrize("abort", [False, True])
def test_cli_surface_selector_runs_responses_and_rejects_http_200_abort(tmp_path, abort):
    with FakeFleet(abort_finish_reason=abort) as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify",
            "--profile",
            str(profile),
            "--surface",
            "responses",
            "--local-output",
            str(run_dir),
            "--pod-logs",
            "off",
            "--evidence",
            "all",
            "-k",
            "test_responses_basic",
        )
    assert result.returncode == (1 if abort else 0), result.stdout + result.stderr
    (record,) = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    assert record["case_id"] == CASE
    assert record["surface"] == "responses"
    assert record["outcome"] == ("fail" if abort else "pass")


def test_native_contract_failure_is_redacted_in_failure_and_journal():
    secret = "llmdv-response-error-secret"
    previous = redact.known_secrets()
    active = journal.activate("responses-redaction")
    redact.install_secrets([secret])
    try:
        body = response_body()
        body["output"][0]["role"] = secret
        with pytest.raises(AssertionError) as exc:
            assert_response_contract(body)
        assert len(active.checks) == 1
        assert active.checks[0].name == "response_message_role"
        for output in (str(exc.value), json.dumps([c.to_dict() for c in active.checks])):
            assert secret not in output
            assert redact.SENTINEL in output
    finally:
        journal.deactivate()
        redact.reset_secrets()
        redact.install_secrets(previous)


def test_native_contract_journals_one_summary_regardless_of_output_size():
    body = response_body()
    body["output"] *= 256
    active = journal.activate("responses-contract-summary")
    try:
        assert_response_contract(body)
        (check,) = active.checks
        assert check.name == "response_contract"
        assert check.ok
        assert check.observed == {
            "output_items": 256,
            "messages": 256,
            "usage": body["usage"],
        }
    finally:
        journal.deactivate()


@pytest.mark.parametrize(
    "leak,field,passes",
    [
        (None, None, True),
        ("glued", None, True),  # Inline reasoning is the declared shape on this profile.
        ("glued", "reasoning_content", False),
        ("unopened", None, False),
        ("untagged", None, False),
    ],
)
def test_basic_output_text_is_the_answer_alone(tmp_path, leak, field, passes):
    with FakeFleet(answer_leak=leak) as fleet:
        path = write_profile(tmp_path, fleet)
        if field:
            raw = yaml.safe_load(path.read_text())
            raw["model"]["reasoning"] = {"parser": "example", "field": field, "default_on": True}
            path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_responses_basic")
    assert code == (0 if passes else 1), records
    (record,) = records
    assert record["case_id"] == CASE
    assert next(c for c in record["checks"] if c["name"] == "answer_only")["ok"] is passes
