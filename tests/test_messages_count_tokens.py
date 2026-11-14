"""Exercise the shipped token-count cases against a fleet that can lie about counts."""

from __future__ import annotations

import json

import pytest
import yaml

from tests.fakes import COUNT_TOKENS_PATH, FakeFleet, run_llmdv
from tests.test_suites_against_fake import run_suites, write_profile

RAW_CASE = "anthropic_messages.count_tokens"
SDK_CASE = "anthropic_messages.count_tokens_parses_with_anthropic_sdk"
UNKNOWN_CASE = "anthropic_messages.count_tokens_unknown_model"
INVALID_CASE = "anthropic_messages.count_tokens_invalid_messages"
HAPPY = "test_messages_count_tokens and not rejects"
ALL = "test_messages_count_tokens"


def exchanges(run_dir):
    return [json.loads(line) for line in (run_dir / "exchanges.jsonl").read_text().splitlines()]


def test_count_contract_runs_per_ingress_with_raw_and_sdk_evidence(tmp_path):
    with FakeFleet() as direct, FakeFleet() as router:
        profile = write_profile(tmp_path, direct)
        config = yaml.safe_load(profile.read_text())
        config["ingresses"].append(
            {**config["ingresses"][0], "id": "router", "base_url": router.base_url}
        )
        profile.write_text(yaml.safe_dump(config))
        run_dir = tmp_path / "run"
        code, records = run_suites(profile, run_dir, "-k", ALL, "--llmdv-evidence", "all")

    assert code == 0, records
    assert len(records) == 10
    assert {(r["case_id"], r["ingress"]) for r in records} == {
        (case, ingress)
        for case in (RAW_CASE, SDK_CASE, UNKNOWN_CASE, INVALID_CASE)
        for ingress in ("fake", "router")
    }
    for record in records:
        assert record["outcome"] == "pass", record
        assert record["checks"] and all(check["ok"] for check in record["checks"])
        assert (run_dir / record["evidence"]).is_dir()
    traffic = exchanges(run_dir)
    assert len(traffic) == 12
    for exchange in traffic:
        assert exchange["url"].endswith(COUNT_TOKENS_PATH)
        assert "/v1/v1/" not in exchange["url"]
        assert exchange["request_headers"]["anthropic-version"] == "2023-06-01"
        assert not {"max_tokens", "temperature", "stream"} & exchange["request_body"].keys()
    for ingress in ("fake", "router"):
        raw = [e for e in traffic if e["case_id"] == RAW_CASE and e["ingress"] == ingress]
        sdk = [e for e in traffic if e["case_id"] == SDK_CASE and e["ingress"] == ingress]
        assert raw[0]["request_body"] == sdk[0]["request_body"]
        assert raw[1]["request_body"]["system"] == raw[0]["request_body"]["system"]
        assert raw[1]["request_body"]["messages"][:1] == raw[0]["request_body"]["messages"]
        assert len(raw[1]["request_body"]["messages"]) == 3


@pytest.mark.parametrize(
    "response",
    [{}, {"input_tokens": None}, {"input_tokens": True}, {"input_tokens": "3"},
     {"input_tokens": -1}, {"input_tokens": 1.5}],
    ids=["missing", "null", "boolean", "string", "negative", "fractional"],
)  # fmt: skip
def test_raw_and_sdk_reject_invalid_counts_on_http_200(tmp_path, response):
    with FakeFleet(count_tokens_response=response) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", HAPPY)

    assert code == 1
    assert {r["case_id"] for r in records} == {RAW_CASE, SDK_CASE}
    for record in records:
        assert record["outcome"] == "fail", record
        assert record["http_status"] == 200
        assert any(c["name"] == "input_tokens" and not c["ok"] for c in record["checks"])


def test_sdk_accepts_zero_and_additional_response_fields(tmp_path):
    """Zero is a valid individual count; growth is checked separately by the raw case."""
    with FakeFleet(count_tokens_response={"input_tokens": 0, "vendor_field": "extra"}) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", f"{HAPPY} and sdk")

    assert code == 0, records
    (record,) = records
    assert record["case_id"] == SDK_CASE and record["outcome"] == "pass"


@pytest.mark.parametrize(
    "options",
    [
        {"decreasing_token_count": True},
        {"count_tokens_response": {"input_tokens": 0}},
        {"count_tokens_response": {"input_tokens": 17}},
    ],
    ids=["decreasing", "constant-zero", "constant-positive"],
)
def test_counts_that_do_not_grow_fail_with_both_requests_recorded(tmp_path, options):
    with FakeFleet(**options) as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        code, records = run_suites(profile, run_dir, "-k", f"{HAPPY} and not sdk")

    assert code == 1
    (record,) = records
    assert record["outcome"] == "fail"
    check = next(c for c in record["checks"] if c["name"] == "input_tokens_grow")
    assert not check["ok"]
    assert check["observed"]["expanded"] <= check["observed"]["baseline"]
    assert len(exchanges(run_dir)) == 2


@pytest.mark.parametrize("options", [{"non_json_success": True}, {"serve_count_tokens": False}])
def test_non_json_success_and_missing_declared_endpoint_fail(tmp_path, options):
    with FakeFleet(**options) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", HAPPY)

    assert code == 1
    assert len(records) == 2 and all(r["outcome"] == "fail" for r in records)
    raw = next(r for r in records if r["case_id"] == RAW_CASE)
    assert any(not c["ok"] for c in raw["checks"])


@pytest.mark.parametrize(
    ("options", "keyword", "expected_rows"),
    [
        ({"fall_back_on_unknown_model": True}, "unknown_model", 1),
        ({"ignore_bad_count_tokens": True}, "invalid_messages", 2),
    ],
)
def test_silent_acceptance_of_invalid_requests_fails(tmp_path, options, keyword, expected_rows):
    with FakeFleet(**options) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", f"{ALL} and {keyword}")

    assert code == 1
    assert len(records) == expected_rows
    for record in records:
        assert record["outcome"] == "fail"
        assert record["http_status"] == 200
        assert any(not c["ok"] for c in record["checks"])


@pytest.mark.parametrize("declared", [False, True])
def test_error_envelope_follows_the_ingress_declaration(tmp_path, declared):
    classes = ("unknown_model", "missing_required_field", "wrong_field_type")
    with FakeFleet(plain_text_errors=True) as fleet:
        profile = write_profile(tmp_path, fleet, unstructured_errors=classes if declared else ())
        code, records = run_suites(profile, tmp_path / "run", "-k", f"{ALL} and rejects")

    assert code == (0 if declared else 1)
    assert len(records) == 3
    for record in records:
        assert record["outcome"] == ("pass" if declared else "fail")
        check = next(c for c in record["checks"] if c["name"] == "error_envelope")
        assert check["ok"] is declared


@pytest.mark.parametrize("missing", ["capability", "false", "surface"])
def test_undeclared_counting_skips_before_any_request(tmp_path, missing):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        config = yaml.safe_load(profile.read_text())
        if missing == "capability":
            del config["capabilities"]
        elif missing == "false":
            config["capabilities"]["count_tokens"] = False
        else:
            config["ingresses"][0]["surfaces"] = ["chat_completions"]
        profile.write_text(yaml.safe_dump(config))
        code, records = run_suites(profile, tmp_path / "run", "-k", ALL)
        assert not fleet.seen

    assert code == 0
    assert len(records) == 5
    assert all((r["outcome"], r["skip_class"]) == ("skip", "undeclared") for r in records)


def test_shared_fleet_excludes_only_the_malformed_count_requests(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet, fleet_policy="shared")
        code, records = run_suites(profile, tmp_path / "run", "-k", ALL)
        assert len(fleet.seen) == 4  # two raw counts, one SDK count and an unknown model

    assert code == 0, records
    assert len(records) == 5
    for record in records:
        if record["case_id"] == INVALID_CASE:
            assert (record["outcome"], record["skip_class"]) == ("skip", "excluded_by_policy")
        else:
            assert record["outcome"] == "pass"


@pytest.mark.parametrize("declared", [False, True])
def test_count_requests_respect_declared_throttling_without_retries(tmp_path, declared):
    with FakeFleet(throttle=True) as fleet:
        profile = write_profile(tmp_path, fleet, rate_limited=declared)
        code, records = run_suites(profile, tmp_path / "run", "-k", ALL)
        assert len(fleet.seen) == 5

    assert code == (0 if declared else 1)
    assert len(records) == 5
    for record in records:
        assert record["outcome"] == ("skip" if declared else "fail")
        if declared:
            assert record["skip_class"] == "throttled"


@pytest.mark.parametrize("auth_type", ["bearer", "x-api-key"])
def test_agent_cli_counts_with_profile_auth_and_redacts_evidence(tmp_path, auth_type):
    secret = "llmdv-count-tokens-test-secret"
    header = (
        ("Authorization", f"Bearer {secret}") if auth_type == "bearer" else ("x-api-key", secret)
    )
    with FakeFleet(auth_header=header) as fleet:
        profile = write_profile(tmp_path, fleet)
        config = yaml.safe_load(profile.read_text())
        config["ingresses"][0]["auth"] = {"type": auth_type, "key_env": "LLMDV_COUNT_TOKENS_KEY"}
        profile.write_text(yaml.safe_dump(config))
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify", "--profile", str(profile), "--client", "agent",
            "--local-output", str(run_dir), "--pod-logs", "off", "--evidence", "all",
            "-k", HAPPY, env={"LLMDV_COUNT_TOKENS_KEY": secret},
        )  # fmt: skip

    assert result.returncode == 0, result.stdout + result.stderr
    traffic = exchanges(run_dir)
    assert len(traffic) == 3
    assert {e["case_id"] for e in traffic} == {RAW_CASE, SDK_CASE}
    for _, path, headers in fleet.seen:
        assert path == COUNT_TOKENS_PATH
        assert {k.lower(): v for k, v in headers.items()}[header[0].lower()] == header[1]
    for path in run_dir.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".log", ".md"}:
            assert secret not in path.read_text(), path
