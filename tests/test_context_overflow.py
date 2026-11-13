"""Bounded overflow probes must distinguish token limits from intermediary refusals."""

from __future__ import annotations

import json

import pytest

from llmdv.assertions import assert_context_limit_error
from llmdv.evidence import journal
from llmdv.http import redact
from llmdv.http.client import Exchange
from llmdv.profile.schema import Capabilities, Ingress, IngressKind, Model
from llmdv.suites.unhappy import test_error_paths as unhappy
from llmdv.surfaces.chat_completions import CHAT_COMPLETIONS
from tests.fakes import MAX_MODEL_LEN, FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile

INGRESS = Ingress(
    id="test",
    kind=IngressKind.VLLM,
    base_url="http://unused",
    model=Model(public_id="m"),
    capabilities=Capabilities(),
)


@pytest.fixture(params=["chat_completions", "completions", "messages", "responses"])
def surface_args(request):
    return ("--llmdv-surface", request.param)


@pytest.mark.parametrize(
    "message",
    [
        "This model's maximum context length is 40,960 tokens. You requested 61440 tokens.",
        "Maximum context length is 40960 tokens, but the input has 61440 tokens.",
        "Maximum context window: 40960 tokens; reduce the input.",
    ],
)
def test_context_limit_cause_does_not_depend_on_whole_error_wording(message):
    exchange = Exchange(
        "POST", "/v1/chat/completions", status=400, response_body={"error": {"message": message}}
    )
    assert_context_limit_error(exchange, CHAT_COMPLETIONS, INGRESS, 40960)


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (413, "Request body exceeds the maximum size of 40960 bytes"),
        (400, "Invalid request; requested 40960 tokens"),
        (401, "Invalid API key"),
        (400, "This model's maximum context length is 20480 tokens"),
        (200, "This model's maximum context length is 40960 tokens"),
        (500, "This model's maximum context length is 40960 tokens"),
        (400, None),
    ],
)
def test_unrelated_errors_stale_limits_and_non_4xx_cannot_prove_overflow(status, message):
    exchange = Exchange(
        "POST", "/v1/chat/completions", status=status, response_body={"error": {"message": message}}
    )
    with pytest.raises(AssertionError, match="did not identify the declared context limit"):
        assert_context_limit_error(exchange, CHAT_COMPLETIONS, INGRESS, 40960)


def test_rejection_evidence_and_failure_messages_scrub_credentials():
    secret = "llmdv-secret-in-error"
    prior_secrets = redact.known_secrets()
    active = journal.activate("context-overflow-redaction")
    redact.install_secrets([secret])
    try:
        exchange = Exchange(
            "POST",
            "/v1/chat/completions",
            status=403,
            response_body={"error": {"message": f"Rejected key {secret}, Bearer another-token"}},
        )
        with pytest.raises(AssertionError) as exc:
            assert_context_limit_error(exchange, CHAT_COMPLETIONS, INGRESS, 40960)
        evidence = json.dumps([check.to_dict() for check in active.checks])
        for output in (str(exc.value), evidence):
            assert secret not in output
            assert "another-token" not in output
            assert redact.SENTINEL in output
    finally:
        journal.deactivate()
        redact.reset_secrets()
        redact.install_secrets(prior_secrets)


@pytest.mark.parametrize("plain_text", [False, True])
def test_a_real_context_rejection_passes_on_all_surfaces_with_evidence(tmp_path, plain_text):
    with FakeFleet(plain_text_errors=plain_text) as fleet:
        profile = write_profile(
            tmp_path,
            fleet,
            unstructured_errors=("context_overflow",) if plain_text else (),
        )
        code, records = run_suites(profile, tmp_path / "run", "-k", "context_overflow")

    assert code == 0
    assert {r["surface"] for r in records} == {
        "chat_completions",
        "completions",
        "messages",
        "responses",
    }
    for record in records:
        assert record["outcome"] == "pass"
        checks = {c["name"]: c for c in record["checks"]}
        assert checks["context_limit_rejection"]["ok"]
        assert checks["context_limit_rejection"]["observed"]["reported_limits"] == [
            str(MAX_MODEL_LEN)
        ]
        assert checks["context_overflow_projected_body_bytes"]["observed"] <= (
            unhappy.CONTEXT_OVERFLOW_MAX_BODY_BYTES
        )


@pytest.mark.parametrize("status", [400, 413])
@pytest.mark.parametrize("plain_text", [False, True])
def test_an_intermediary_body_rejection_fails_even_with_a_declared_envelope_deviation(
    tmp_path, status, plain_text, surface_args
):
    with FakeFleet(
        overflow_error=(status, "Request body exceeds the maximum size of 1024 bytes"),
        plain_text_errors=plain_text,
    ) as fleet:
        profile = write_profile(
            tmp_path,
            fleet,
            unstructured_errors=("context_overflow",) if plain_text else (),
        )
        code, records = run_suites(
            profile, tmp_path / "run", *surface_args, "-k", "context_overflow"
        )

    assert code == 1
    (record,) = records
    assert record["outcome"] == "fail"
    assert record["http_status"] == status
    assert any(c["name"] == "context_limit_rejection" and not c["ok"] for c in record["checks"])


@pytest.mark.parametrize("max_model_len", [40960, 131072])
def test_reference_and_128k_windows_run_within_the_byte_cap(tmp_path, max_model_len, surface_args):
    with FakeFleet(max_model_len=max_model_len) as fleet:
        profile = write_profile(tmp_path, fleet, max_model_len=max_model_len)
        code, records = run_suites(
            profile, tmp_path / "run", *surface_args, "-k", "context_overflow"
        )
        ((_, _, headers),) = fleet.seen

    assert code == 0
    assert records[0]["outcome"] == "pass"
    checks = {c["name"]: c["observed"] for c in records[0]["checks"]}
    assert int(headers["Content-Length"]) == checks["context_overflow_projected_body_bytes"]
    assert int(headers["Content-Length"]) < 660000  # ~205 KB and ~656 KB with a fixed margin.


def test_a_million_token_window_skips_without_sending_an_overflow_request(tmp_path, surface_args):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet, max_model_len=1048576)
        run_dir = tmp_path / "run"
        code, records = run_suites(profile, run_dir, *surface_args, "-k", "context_overflow")
        assert not fleet.seen

    assert code == 0
    (record,) = records
    assert (record["outcome"], record["skip_class"]) == ("skip", "not_implemented")
    assert "1048576-byte body cap" in record["reason"]
    assert (run_dir / "exchanges.jsonl").read_text() == ""
    checks = {c["name"]: c["observed"] for c in record["checks"]}
    assert checks["declared_max_model_len"] == 1048576
    assert (
        checks["context_overflow_projected_body_bytes"] > checks["context_overflow_body_cap_bytes"]
    )


def test_silent_truncation_records_a_failed_check(tmp_path, surface_args):
    with FakeFleet(truncate_overflow=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        code, records = run_suites(profile, run_dir, *surface_args, "-k", "context_overflow")

    assert code == 1
    (record,) = records
    assert record["outcome"] == "fail"
    assert any(c["name"] == "context_overflow_generation" and not c["ok"] for c in record["checks"])
    (exchange,) = [
        json.loads(line) for line in (run_dir / "exchanges.jsonl").read_text().splitlines()
    ]
    assert exchange["status"] == 200
