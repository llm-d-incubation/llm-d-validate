"""A reasoning-only HTTP 200 must fail the declared answer-budget contract."""

from __future__ import annotations

import json

import pytest
import yaml

from tests.fakes import FakeFleet, leaked_answer, run_llmdv
from tests.test_suites_against_fake import SUITES, run_pytest, run_suites, write_profile

BUDGET = 37  # Deliberately different from budget_for's reasoning-model default.
CASES = {"openai_chat.answer_budget", "anthropic_messages.answer_budget"}


def budget_profile(tmp_path, fleet, budget=BUDGET, **kwargs):
    path = write_profile(tmp_path, fleet, **kwargs)
    config = yaml.safe_load(path.read_text())
    config["model"]["answer_max_tokens"] = budget
    path.write_text(yaml.safe_dump(config))
    return path


def exchanges(run_dir):
    return [json.loads(line) for line in (run_dir / "exchanges.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("entry_point", ["pytest", "verify", "validate", "show"])
def test_invalid_budget_is_a_profile_error_before_any_request(tmp_path, entry_point):
    with FakeFleet() as fleet:
        profile = budget_profile(tmp_path, fleet, budget="256")
        run_dir = tmp_path / "run"
        if entry_point == "pytest":
            result, records = run_pytest(SUITES, profile, run_dir, "-k", "answer_budget")
            expected_code = pytest.ExitCode.USAGE_ERROR
            assert records == []
        elif entry_point == "verify":
            result = run_llmdv(
                "verify",
                "--profile",
                str(profile),
                "--local-output",
                str(run_dir),
                "--pod-logs",
                "off",
                "-k",
                "answer_budget",
            )
            expected_code = 2
        else:
            result = run_llmdv("profile", entry_point, str(profile))
            expected_code = 2

        assert result.returncode == expected_code, result.stdout + result.stderr
        assert "model.answer_max_tokens: expected a positive integer, got '256'" in result.stderr
        assert "INTERNALERROR" not in result.stderr
        assert not fleet.seen


def response_shapes(shape, answer="", reasoning="still thinking", limited=True):
    """The same generation as native reasoning or inline tags, on both wire surfaces."""
    message = {"role": "assistant", "content": answer, "reasoning_content": reasoning}
    blocks = [{"type": "thinking", "thinking": reasoning}, {"type": "text", "text": answer}]
    if shape != "native":
        closing = "</think>" if shape == "inline" else ""
        content = f"<think>{reasoning}{closing}{answer}"
        message = {"role": "assistant", "content": content}
        blocks = [{"type": "text", "text": content}]
    return {
        "chat_overrides": {
            "choices": [
                {"index": 0, "message": message, "finish_reason": "length" if limited else "stop"}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": BUDGET, "total_tokens": BUDGET + 3},
        },
        "message_overrides": {
            "content": blocks,
            "stop_reason": "max_tokens" if limited else "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": BUDGET},
        },
    }


@pytest.mark.parametrize("shape", ["native", "inline"])
def test_answers_pass_even_at_the_limit_with_lengths_and_exact_budget_recorded(tmp_path, shape):
    with FakeFleet(**response_shapes(shape, answer="ok")) as fleet:
        profile = budget_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        code, records = run_suites(
            profile, run_dir, "-k", "answer_budget", "--llmdv-evidence", "all"
        )
        assert len(fleet.seen) == 2

    assert code == 0, records
    assert {r["case_id"] for r in records} == CASES
    for record in records:
        assert record["outcome"] == "pass"
        assert next(c for c in record["checks"] if c["name"] == "answer_only")["ok"]
        check = next(c for c in record["checks"] if c["name"] == "answer_budget")
        assert check["ok"]
        assert check["observed"] == {
            "max_tokens": BUDGET,
            "answer_chars": 2,
            "reasoning_chars": len("still thinking"),
            "output_tokens": BUDGET,
            "finish_reason": "length" if record["surface"] == "chat_completions" else "max_tokens",
        }
        assert (run_dir / record["evidence"]).is_dir()
    for exchange in exchanges(run_dir):
        assert exchange["request_body"] == {
            "model": fleet.model,
            "max_tokens": BUDGET,
            "temperature": 0,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        }


@pytest.mark.parametrize(
    "shape,leak,field",
    [
        ("native", "untagged", None),
        ("native", "unopened", None),
        ("native", "glued", None),
        ("inline", None, "reasoning_content"),
    ],
)
def test_reasoning_in_the_answer_fails_while_existing_smokes_still_pass(
    tmp_path, shape, leak, field
):
    shapes = response_shapes(shape, answer=leaked_answer("ok", leak), limited=False)
    with FakeFleet(**shapes) as fleet:
        profile = budget_profile(tmp_path, fleet)
        if field:
            config = yaml.safe_load(profile.read_text())
            config["model"]["reasoning"] = {"parser": "example", "field": field, "default_on": True}
            profile.write_text(yaml.safe_dump(config))
        code, records = run_suites(
            profile,
            tmp_path / "run",
            "-k",
            "answer_budget or test_chat_basic or test_messages_basic",
        )

    assert code == 1, records
    assert len(records) == 6
    for record in records:
        if record["case_id"] not in CASES:
            assert record["outcome"] == "pass", record
            continue
        assert record["outcome"] == "fail"
        assert "does not carry the answer alone" in record["reason"]
        assert next(c for c in record["checks"] if c["name"] == "answer_budget")["ok"]
        assert not next(c for c in record["checks"] if c["name"] == "answer_only")["ok"]


@pytest.mark.parametrize("shape", ["native", "inline", "unterminated-inline"])
def test_exhaustion_fails_while_existing_smokes_still_pass(tmp_path, shape):
    with FakeFleet(**response_shapes(shape)) as fleet:
        profile = budget_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        code, records = run_suites(
            profile, run_dir, "-k", "answer_budget or test_chat_basic or test_messages_basic"
        )
        assert len(fleet.seen) == 6  # One request per case, including the two SDK smokes.

    assert code == 1, records
    assert len(records) == 6
    for record in records:
        if record["case_id"] not in CASES:
            assert record["outcome"] == "pass", record
            continue
        assert record["outcome"] == "fail"
        assert record["http_status"] == 200
        assert "output budget exhausted before an answer" in record["reason"]
        assert f"max_tokens={BUDGET}" in record["reason"]
        check = next(c for c in record["checks"] if c["name"] == "answer_budget")
        assert not check["ok"]
        assert check["observed"]["answer_chars"] == 0
        assert check["observed"]["reasoning_chars"] == len("still thinking")
        assert (run_dir / record["evidence"] / "verdict.txt").is_file()


def test_whitespace_answer_fails_without_misdiagnosing_a_normal_stop_as_exhaustion(tmp_path):
    with FakeFleet(**response_shapes("native", answer=" \n\t", limited=False)) as fleet:
        profile = budget_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "answer_budget")

    assert code == 1
    assert len(records) == 2
    for record in records:
        assert record["outcome"] == "fail"
        assert "no answer text returned" in record["reason"]
        assert "budget exhausted" not in record["reason"]
        assert any(c["name"] == "answer_budget" and not c["ok"] for c in record["checks"])


def test_answer_that_exceeds_the_requested_limit_does_not_prove_budget_sufficiency(tmp_path):
    options = response_shapes("native", answer="ok")
    options["chat_overrides"]["usage"] = {
        "prompt_tokens": 3,
        "completion_tokens": BUDGET + 1,
        "total_tokens": BUDGET + 4,
    }
    options["message_overrides"]["usage"]["output_tokens"] = BUDGET + 1
    with FakeFleet(**options) as fleet:
        profile = budget_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "answer_budget")

    assert code == 1
    assert len(records) == 2
    for record in records:
        assert record["outcome"] == "fail"
        assert "beyond the declared output budget" in record["reason"]
        check = next(c for c in record["checks"] if c["name"] == "answer_budget")
        assert not check["ok"]
        assert check["observed"]["output_tokens"] == BUDGET + 1


def test_nonreasoning_answers_run_on_shared_fleets_and_each_ingress(tmp_path):
    with FakeFleet(inline_thinking=False) as direct, FakeFleet(inline_thinking=False) as router:
        profile = budget_profile(tmp_path, direct, fleet_policy="shared")
        config = yaml.safe_load(profile.read_text())
        del config["model"]["reasoning"]
        config["ingresses"].append(
            {**config["ingresses"][0], "id": "router", "base_url": router.base_url}
        )
        profile.write_text(yaml.safe_dump(config))
        code, records = run_suites(profile, tmp_path / "run", "-k", "answer_budget")
        assert len(direct.seen) == len(router.seen) == 2

    assert code == 0, records
    assert len(records) == 4
    assert {(r["case_id"], r["ingress"]) for r in records} == {
        (case, ingress) for case in CASES for ingress in ("fake", "router")
    }
    for record in records:
        assert record["outcome"] == "pass"
        check = next(c for c in record["checks"] if c["name"] == "answer_budget")
        assert check["observed"]["answer_chars"] == 2
        assert check["observed"]["reasoning_chars"] == 0


@pytest.mark.parametrize("missing", ["budget", "null-budget", "surfaces"])
def test_undeclared_contract_skips_before_sending_any_request(tmp_path, missing):
    with FakeFleet() as fleet:
        profile = budget_profile(tmp_path, fleet)
        config = yaml.safe_load(profile.read_text())
        if missing == "budget":
            del config["model"]["answer_max_tokens"]
        elif missing == "null-budget":
            config["model"]["answer_max_tokens"] = None
        else:
            config["ingresses"][0]["surfaces"] = ["completions", "responses"]
            config["clients"] = []
        profile.write_text(yaml.safe_dump(config))
        code, records = run_suites(profile, tmp_path / "run", "-k", "answer_budget")
        assert not fleet.seen

    assert code == 0, records
    assert len(records) == 2
    assert all((r["outcome"], r["skip_class"]) == ("skip", "undeclared") for r in records)


@pytest.mark.parametrize(
    ("options", "check_name"),
    [
        ({"abort_finish_reason": True}, "finish_reason"),
        ({"non_json_success": True}, "response_json_object"),
        (
            {"chat_overrides": {"model": "wrong"}, "message_overrides": {"model": "wrong"}},
            "model_echo",
        ),
        ({"chat_overrides": {"usage": {}}, "message_overrides": {"usage": {}}}, "usage"),
    ],
)
def test_budget_case_keeps_the_existing_generation_checks(tmp_path, options, check_name):
    with FakeFleet(**options) as fleet:
        profile = budget_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "answer_budget")

    assert code == 1
    assert len(records) == 2
    for record in records:
        assert record["outcome"] == "fail", record
        assert any(c["name"] == check_name and not c["ok"] for c in record["checks"])


@pytest.mark.parametrize("declared", [False, True])
def test_throttling_respects_the_declaration_without_retrying(tmp_path, declared):
    with FakeFleet(throttle=True) as fleet:
        profile = budget_profile(tmp_path, fleet, rate_limited=declared)
        code, records = run_suites(profile, tmp_path / "run", "-k", "answer_budget")
        assert len(fleet.seen) == 2

    assert code == (0 if declared else 1)
    assert len(records) == 2
    for record in records:
        assert record["outcome"] == ("skip" if declared else "fail")
        if declared:
            assert record["skip_class"] == "throttled"


@pytest.mark.parametrize("auth_type", ["bearer", "x-api-key"])
def test_agent_cli_selects_both_cases_with_authentication_and_redacted_evidence(
    tmp_path, auth_type
):
    secret = "llmdv-answer-budget-test-secret"
    header = (
        ("Authorization", f"Bearer {secret}") if auth_type == "bearer" else ("x-api-key", secret)
    )
    with FakeFleet(auth_header=header) as fleet:
        profile = budget_profile(tmp_path, fleet)
        config = yaml.safe_load(profile.read_text())
        config["ingresses"][0]["auth"] = {"type": auth_type, "key_env": "LLMDV_BUDGET_KEY"}
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
            "answer_budget",
            env={"LLMDV_BUDGET_KEY": secret},
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert {e["case_id"] for e in exchanges(run_dir)} == CASES
    assert len(fleet.seen) == 2
    for _, _, headers in fleet.seen:
        assert {k.lower(): v for k, v in headers.items()}[header[0].lower()] == header[1]
    for path in run_dir.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".log", ".md", ".txt", ".sh"}:
            assert secret not in path.read_text(), path
