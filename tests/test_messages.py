"""Messages must preserve its wire contract through raw HTTP and the pinned SDK."""

from __future__ import annotations

import json

import pytest
import yaml

from llmdv.surfaces import get
from tests.fakes import FakeFleet, run_llmdv
from tests.test_suites_against_fake import run_suites, write_profile

MESSAGES = get("messages")
CASES = {
    "anthropic_messages.basic_non_streaming",
    "anthropic_messages.basic_parses_with_anthropic_sdk",
}


@pytest.mark.parametrize(
    ("blocks", "text", "reasoning"),
    [
        ([{"type": "text", "text": "o"}, {"type": "text", "text": "k"}], "ok", ""),
        (
            [
                {"type": "thinking", "thinking": "working", "signature": "sig"},
                {"type": "text", "text": "ok"},
            ],
            "ok",
            "working",
        ),
        ([{"type": "text", "text": "<think>working</think>ok"}], "ok", "working"),
        ([{"type": "text", "text": "<think>still working"}], "", "still working"),
    ],
)
def test_content_blocks_and_native_usage_are_preserved(blocks, text, reasoning):
    body = {
        "type": "message",
        "role": "assistant",
        "model": "m",
        "content": blocks,
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": 10},
    }
    completion = MESSAGES.parse_completion(body)
    assert completion.text == text
    assert completion.reasoning_text == reasoning
    assert completion.has_output
    assert completion.finish_reason == "max_tokens"
    assert completion.usage.prompt_tokens == 3
    assert completion.usage.completion_tokens == 2
    assert completion.usage.total_tokens is None  # this API does not report a total
    assert completion.raw == body


def test_messages_errors_require_the_anthropic_envelope():
    detail = {"type": "invalid_request_error", "message": "max_tokens must be positive"}
    body = {"type": "error", "error": detail, "request_id": "req_fake"}
    error = MESSAGES.parse_error(body)
    assert error is not None
    assert (error.type, error.message, error.raw) == (detail["type"], detail["message"], body)
    for malformed in (None, "bad request", {"error": detail}, {"type": "error", "error": "bad"}):
        assert MESSAGES.parse_error(malformed) is None
    assert MESSAGES.looks_like_success({"type": "message", "content": []})
    assert not MESSAGES.looks_like_success(body)


@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens", "stop_sequence"])
def test_raw_and_sdk_smokes_run_through_every_ingress_with_evidence(tmp_path, stop_reason):
    with FakeFleet(message_overrides={"stop_reason": stop_reason}) as direct, FakeFleet() as router:
        profile = write_profile(tmp_path, direct)
        config = yaml.safe_load(profile.read_text())
        config["ingresses"].append(
            {**config["ingresses"][0], "id": "router", "base_url": router.base_url}
        )
        profile.write_text(yaml.safe_dump(config))
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_messages_basic")

    assert code == 0, records
    assert {(r["case_id"], r["ingress"]) for r in records} == {
        (case, ingress) for case in CASES for ingress in ("fake", "router")
    }
    for record in records:
        assert record["outcome"] == "pass"
        assert record["http_status"] == 200
        checks = {c["name"]: c for c in record["checks"]}
        assert all(
            checks[k]["ok"] for k in ("object", "role", "model_echo", "output_chars", "usage")
        )
        assert checks["finish_reason"]["observed"] == (
            stop_reason if record["ingress"] == "fake" else "end_turn"
        )
    exchanges = [
        json.loads(line) for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    assert len(exchanges) == 4
    for exchange in exchanges:
        assert exchange["url"].endswith("/v1/messages")
        assert "/v1/v1/" not in exchange["url"]
        assert exchange["request_headers"]["anthropic-version"] == "2023-06-01"
        assert exchange["request_body"]["messages"][0]["role"] == "user"
        assert exchange["request_body"]["temperature"] == 0
        assert not exchange["request_body"].get("stream")
        assert exchange["response_body"]["usage"] == {"input_tokens": 3, "output_tokens": 2}
        assert exchange["elapsed_s"] > 0


@pytest.mark.parametrize(
    ("overrides", "failed_check"),
    [
        ({"type": "chat.completion"}, "object"),
        ({"role": "user"}, "role"),
        ({"model": "another/model"}, "model_echo"),
        ({"stop_reason": "abort"}, "finish_reason"),
        ({"stop_reason": None}, "finish_reason_recognised"),
        ({"content": []}, "output_chars"),
        ({"content": "invalid blocks"}, "output_chars"),
        ({"content": [None, "not a block"]}, "output_chars"),
        ({"content": [{"type": "text", "text": 42}]}, "output_chars"),
        ({"content": [{"type": "thinking", "thinking": False}]}, "output_chars"),
        ({"usage": None}, "usage"),
        ({"usage": "invalid usage"}, "usage"),
        ({"usage": {"input_tokens": 3}}, "usage"),
        ({"usage": {"output_tokens": 2}}, "usage"),
    ],
)
def test_raw_and_sdk_smokes_reject_malformed_http_200(tmp_path, overrides, failed_check):
    with FakeFleet(message_overrides=overrides) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_messages_basic")

    assert code == 1
    assert {r["case_id"] for r in records} == CASES
    for record in records:
        assert record["outcome"] == "fail", record
        assert record["http_status"] == 200
        assert any(c["name"] == failed_check and not c["ok"] for c in record["checks"])


@pytest.mark.parametrize("auth_type", ["none", "x-api-key", "bearer"])
def test_agent_cli_uses_profile_auth_for_raw_and_sdk_and_redacts_evidence(tmp_path, auth_type):
    secret = "llmdv-messages-test-secret"
    expected = (
        ("Authorization", f"Bearer {secret}")
        if auth_type == "bearer"
        else ("x-api-key", secret)
        if auth_type == "x-api-key"
        else None
    )
    with FakeFleet(auth_header=expected) as fleet:
        profile = write_profile(tmp_path, fleet)
        config = yaml.safe_load(profile.read_text())
        config["ingresses"][0]["auth"] = {"type": auth_type}
        if expected:
            config["ingresses"][0]["auth"]["key_env"] = "LLMDV_MESSAGES_TEST_KEY"
        profile.write_text(yaml.safe_dump(config))
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify",
            "--profile",
            str(profile),
            "--client",
            "agent",
            "--local-output",
            str(run_dir),
            "--pod-logs",
            "off",
            "--evidence",
            "all",
            "-k",
            "test_messages_basic",
            env={
                "LLMDV_MESSAGES_TEST_KEY": secret,
                "ANTHROPIC_API_KEY": "ambient-key",
                "ANTHROPIC_AUTH_TOKEN": "ambient-token",
                "ANTHROPIC_CUSTOM_HEADERS": "x-api-key: ambient-custom-key",
            },
        )

    assert result.returncode == 0, result.stdout + result.stderr
    records = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    assert {r["case_id"] for r in records} == CASES
    assert all(r["outcome"] == "pass" for r in records)
    requests = [headers for _, path, headers in fleet.seen if path == "/v1/messages"]
    assert len(requests) == 2
    for headers in requests:
        headers = {name.lower(): value for name, value in headers.items()}
        if expected:
            assert headers[expected[0].lower()] == expected[1]
        assert not any("ambient" in value for value in headers.values())
    for path in run_dir.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".log", ".md"}:
            assert secret not in path.read_text(), path


def test_messages_smokes_skip_when_the_surface_is_undeclared(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet, surfaces=("chat_completions",))
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_messages_basic")
        assert not fleet.seen

    assert code == 0
    assert {r["case_id"] for r in records} == CASES
    assert all((r["outcome"], r["skip_class"]) == ("skip", "undeclared") for r in records)


@pytest.mark.parametrize("auth_type", ["x-api-key", "bearer"])
def test_missing_profile_secret_never_sends_ambient_sdk_credentials(tmp_path, auth_type):
    expected = (
        ("Authorization", "Bearer not-required")
        if auth_type == "bearer"
        else ("x-api-key", "not-required")
    )
    with FakeFleet(auth_header=expected) as fleet:
        profile = write_profile(tmp_path, fleet)
        config = yaml.safe_load(profile.read_text())
        config["ingresses"][0]["auth"] = {
            "type": auth_type,
            "key_env": "LLMDV_MISSING_KEY",
        }
        profile.write_text(yaml.safe_dump(config))
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify",
            "--profile",
            str(profile),
            "--skip-preflight",
            "--local-output",
            str(run_dir),
            "--pod-logs",
            "off",
            "-k",
            "test_messages_basic_parses_with_pinned_anthropic_sdk",
            env={
                "ANTHROPIC_API_KEY": "ambient-key-must-not-be-sent",
                "ANTHROPIC_AUTH_TOKEN": "ambient-token-must-not-be-sent",
                "ANTHROPIC_CUSTOM_HEADERS": "x-api-key: ambient-custom-key",
            },
        )

    assert result.returncode == 0, result.stdout + result.stderr
    # Check the actual wire headers too: redaction alone could hide a leaked credential.
    ((_, path, headers),) = fleet.seen
    assert path == "/v1/messages"
    headers = {name.lower(): value for name, value in headers.items()}
    assert headers[expected[0].lower()] == expected[1]
    assert not any("ambient" in value for value in headers.values())
    (exchange,) = [
        json.loads(line) for line in (run_dir / "exchanges.jsonl").read_text().splitlines()
    ]
    assert not any("ambient" in value for value in exchange["request_headers"].values())
