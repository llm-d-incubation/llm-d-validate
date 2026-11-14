"""Exposure, connection, retry and credential cases against correct and defective fakes."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
import yaml

from tests.fakes import FakeFleet, run_llmdv
from tests.transport_fakes import Redirector
from tests.transport_helpers import declare, exchanges, outcomes, run


def test_exposure_declarations_hold_on_a_correct_fleet(tmp_path):
    with FakeFleet(transport={"get_paths": {"/version": 200}}) as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_health_status or test_declared_present_paths or test_declared_absent_paths "
            "or test_ignored_headers_on_a_bodiless",
            transport={
                "health_status": [200],
                "present_paths": ["/version", "/v1/models"],
                "absent_paths": ["/openapi.json", "/docs", "/tokenize", "/metrics"],
            },
        )
    for case in (
        "transport.health_status",
        "transport.present_paths",
        "transport.absent_paths",
        "transport.bodiless_header",
    ):
        assert outcomes(by_case, case) == {"pass"}, by_case.get(case)
    assert code == 0
    sent = {(e["method"], e["url"].split("/", 3)[-1]) for e in exchanges(tmp_path)}
    assert all(method == "GET" for method, _ in sent)


@pytest.mark.parametrize(
    "fake,declared,case_id",
    [
        ({"transport": {"get_paths": {"/openapi.json": 200}}},
         {"absent_paths": ["/openapi.json"]}, "transport.absent_paths"),
        ({}, {"present_paths": ["/version"]}, "transport.present_paths"),
        ({"health_status": 503}, {"health_status": [200]}, "transport.health_status"),
        ({"transport": {"defects": {"bodiless_header_honored"}}}, None,
         "transport.bodiless_header"),
    ],
)  # fmt: skip
def test_exposure_defects_fail(tmp_path, fake, declared, case_id):
    select = {
        "transport.absent_paths": "test_declared_absent_paths",
        "transport.present_paths": "test_declared_present_paths",
        "transport.health_status": "test_health_status",
        "transport.bodiless_header": "test_ignored_headers_on_a_bodiless",
    }[case_id]
    with FakeFleet(**fake) as fleet:
        profile = declare(tmp_path, fleet, transport=declared)
        if case_id == "transport.bodiless_header":
            raw = yaml.safe_load(profile.read_text())
            raw["headers"]["x-llmdv-destination"] = {"fake": "ignored"}
            profile.write_text(yaml.safe_dump(raw))
        from tests.test_suites_against_fake import run_suites

        code, records = run_suites(profile, tmp_path / "run", "-k", select)
    assert {r["outcome"] for r in records if r.get("case_id") == case_id} == {"fail"}
    assert code == 1


def test_admin_paths_cannot_be_declared(tmp_path):
    with FakeFleet() as fleet:
        profile = declare(tmp_path, fleet, transport={"absent_paths": ["/is_sleeping"]})
        result = run_llmdv("profile", "validate", str(profile))
    assert result.returncode != 0
    assert "admin route" in result.stdout + result.stderr


@pytest.mark.parametrize("defect,passes", [(None, True), ("close_connections", False)])
def test_connection_reuse(tmp_path, defect, passes):
    with FakeFleet(transport={"defects": {defect} if defect else set()}) as fleet:
        code, by_case = run(tmp_path, fleet, "test_connection_reuse_and_tls")
    assert outcomes(by_case, "transport.connection") == {"pass" if passes else "fail"}
    assert code == (0 if passes else 1)


@pytest.fixture
def self_signed(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("needs openssl")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(  # noqa: S603, S607 - fixed command
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True,
    )  # fmt: skip
    return cert, key


@pytest.mark.parametrize("offered,passes", [(["h2", "http/1.1"], True), (["http/1.1"], False)])
def test_alpn_matches_the_declaration(tmp_path, self_signed, offered, passes):
    with FakeFleet(tls_cert=self_signed, transport={"alpn": offered}) as fleet:
        profile = declare(tmp_path / "p", fleet, transport={"alpn": "h2"})
        raw = yaml.safe_load(profile.read_text())
        raw["ingresses"][0]["tls"] = {"verify": False}
        profile.write_text(yaml.safe_dump(raw))
        from tests.test_suites_against_fake import run_suites

        code, records = run_suites(
            profile, tmp_path / "run", "-k", "test_alpn_selection or test_connection_reuse"
        )
    by_case = {r["case_id"]: r for r in records}
    assert by_case["transport.alpn"]["outcome"] == ("pass" if passes else "fail")
    tls = [c for c in by_case["transport.connection"]["checks"] if c["name"] == "tls_version"]
    assert tls and tls[0]["ok"]
    assert code == (0 if passes else 1)


@pytest.mark.parametrize("redirect", [True, False])
def test_http_redirect(tmp_path, redirect):
    with FakeFleet() as fleet, Redirector("https://127.0.0.1:1", redirect=redirect) as plain:
        code, by_case = run(
            tmp_path, fleet, "test_plain_http_redirects", transport={"http_url": plain.url}
        )
    assert outcomes(by_case, "transport.http_redirect") == {"pass" if redirect else "fail"}
    assert code == (0 if redirect else 1)
    redirect_exchange = next(
        exchange
        for exchange in exchanges(tmp_path)
        if exchange["case_id"] == "transport.http_redirect"
    )
    assert redirect_exchange["request_headers"]["x-llmdv-case-id"] == "transport.http_redirect"
    assert redirect_exchange["request_headers"]["x-llmdv-run-id"]


# A gateway name that resolves nowhere: the plain-http probe reaches the fake listener only
# through HTTP_PROXY, so a derived URL that is not `http://` plus this host never gets an answer.
GATEWAY = "https://gateway.llmdv.invalid"


def declare_redirect(tmp_path, fleet, base_url=GATEWAY, extra_ingress=False, **transport):
    """The fake profile with its ingress moved to `base_url`, reached through a proxy."""
    profile = declare(tmp_path, fleet, transport=transport or {"http_redirect": "expected"})
    raw = yaml.safe_load(profile.read_text())
    gateway = raw["ingresses"][0]
    if extra_ingress:
        # An internal layer reached over plain http that declares no transport at all.
        internal = json.loads(json.dumps(gateway))
        internal.pop("transport")
        internal["id"] = "internal"
        raw["ingresses"].append(internal)
    gateway["base_url"] = base_url
    gateway["setup"] = {"proxy": "allow"}
    profile.write_text(yaml.safe_dump(raw))
    return profile


def run_redirect(tmp_path, profile, plain):
    from tests.test_suites_against_fake import run_suites

    code, records = run_suites(
        profile,
        tmp_path / "run",
        "-k",
        "test_plain_http_redirects",
        "--llmdv-evidence",
        "all",
        env={"HTTP_PROXY": plain.url},
    )
    return code, [r for r in records if r.get("case_id") == "transport.http_redirect"]


@pytest.mark.parametrize(
    "redirect,status", [(True, 301), (False, 200), (False, 503)], ids=["301", "200", "503"]
)
def test_http_redirect_expected_probes_the_ingress_host(tmp_path, redirect, status):
    with FakeFleet() as fleet, Redirector("https://127.0.0.1:1", redirect, status) as plain:
        code, (record,) = run_redirect(tmp_path, declare_redirect(tmp_path, fleet), plain)
    assert record["outcome"] == ("pass" if redirect else "fail")
    (check,) = [c for c in record["checks"] if c["name"] == "http_redirect"]
    assert check["ok"] is redirect
    assert check["observed"]["status"] == status
    assert code == (0 if redirect else 1)
    # The derived address is the base URL's host over http on the default port, and only the
    # exchange records it, as it records base_url.
    (sent,) = [e for e in exchanges(tmp_path) if e["case_id"] == "transport.http_redirect"]
    assert sent["url"] == "http://gateway.llmdv.invalid/health"
    assert "gateway.llmdv.invalid" not in (record.get("reason") or "")


@pytest.mark.parametrize(
    "target,passes", [(GATEWAY, True), ("http://gateway.llmdv.invalid", False)]
)
def test_http_redirect_expected_keeps_the_location_host_out_of_the_record(tmp_path, target, passes):
    """The Location names the gateway too; the check keeps its scheme and path, not its host."""
    with FakeFleet() as fleet, Redirector(target) as plain:
        code, (record,) = run_redirect(tmp_path, declare_redirect(tmp_path, fleet), plain)
    assert record["outcome"] == ("pass" if passes else "fail")
    (check,) = [c for c in record["checks"] if c["name"] == "http_redirect"]
    scheme = target.split("://")[0]
    assert check["observed"]["location"] == f"{scheme}://<host>/health"
    assert "gateway.llmdv.invalid" not in json.dumps(record)
    assert code == (0 if passes else 1)


def test_http_redirect_drops_an_explicit_default_port(tmp_path):
    with FakeFleet() as fleet, Redirector("https://127.0.0.1:1") as plain:
        profile = declare_redirect(tmp_path, fleet, base_url=GATEWAY + ":443/v1")
        code, (record,) = run_redirect(tmp_path, profile, plain)
    assert record["outcome"] == "pass" and code == 0
    (sent,) = [e for e in exchanges(tmp_path) if e["case_id"] == "transport.http_redirect"]
    assert sent["url"] == "http://gateway.llmdv.invalid/health"


@pytest.mark.parametrize("base_url", ["http://gateway.llmdv.invalid", GATEWAY + ":8443"])
def test_an_unprobeable_base_url_from_the_environment_fails_when_preflight_is_skipped(
    tmp_path, base_url
):
    """Preflight refuses it first; a run that skipped preflight must not report a skip."""
    with FakeFleet() as fleet:
        profile = declare_redirect(tmp_path, fleet)
        raw = yaml.safe_load(profile.read_text())
        del raw["ingresses"][0]["base_url"]
        raw["ingresses"][0]["base_url_env"] = "LLMDV_GATEWAY_URL"
        profile.write_text(yaml.safe_dump(raw))
        from tests.test_suites_against_fake import run_suites

        code, records = run_suites(
            profile, tmp_path / "run", "-k", "test_plain_http_redirects",
            env={"LLMDV_GATEWAY_URL": base_url},
        )  # fmt: skip
    (record,) = [r for r in records if r.get("case_id") == "transport.http_redirect"]
    assert record["outcome"] == "fail"
    (check,) = record["checks"]
    assert check["name"] == "http_redirect_base_url" and check["ok"] is False
    assert "gateway.llmdv.invalid" not in json.dumps(record)
    assert code == 1


def test_only_the_ingress_declaring_http_redirect_runs_it(tmp_path):
    with FakeFleet() as fleet, Redirector("https://127.0.0.1:1") as plain:
        profile = declare_redirect(tmp_path, fleet, extra_ingress=True)
        code, records = run_redirect(tmp_path, profile, plain)
    by_ingress = {r["ingress"]: r for r in records}
    assert by_ingress["fake"]["outcome"] == "pass"
    internal = by_ingress["internal"]
    assert internal["outcome"] == "skip" and internal["skip_class"] == "undeclared"
    assert "transport.http_redirect" in internal["reason"]
    assert code == 0


def test_neither_declaration_skips_the_redirect_as_undeclared(tmp_path):
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, "test_plain_http_redirects")
    (record,) = by_case["transport.http_redirect"]
    assert record["outcome"] == "skip" and record["skip_class"] == "undeclared"
    assert code == 0


@pytest.mark.parametrize(
    "defects,passes",
    [(set(), True), ({"should_retry_true"}, False), ({"client_error_503"}, False)],
)
def test_sdk_retry_signal(tmp_path, defects, passes):
    with FakeFleet(transport={"defects": defects}) as fleet:
        code, by_case = run(tmp_path, fleet, "test_deterministic_errors_are_not_retryable")
    assert outcomes(by_case, "transport.sdk_retry_signal") == {"pass" if passes else "fail"}
    assert code == (0 if passes else 1)


@pytest.mark.parametrize(
    "fake,declared,passes",
    [
        ({}, "ignored", True),
        ({"defects": {"idempotency_replay"}}, "ignored", False),
        ({"idempotency": "replayed"}, "replayed", True),
        ({}, "replayed", False),
    ],
)
def test_idempotency_key(tmp_path, fake, declared, passes):
    with FakeFleet(transport=fake) as fleet:
        code, by_case = run(
            tmp_path, fleet, "test_idempotency_key", transport={"idempotency": declared}
        )
    assert outcomes(by_case, "transport.idempotency_key") == {"pass" if passes else "fail"}
    keys = {e["request_headers"].get("idempotency-key") for e in exchanges(tmp_path)}
    assert len(keys) == 1 and None not in keys
    assert code == (0 if passes else 1)


@pytest.mark.parametrize("defects,passes", [(set(), True), ({"retry_rejected"}, False)])
def test_retry_after_timeout(tmp_path, defects, passes):
    with FakeFleet(transport={"slow_first_s": 2.0, "defects": defects}) as fleet:
        code, by_case = run(tmp_path, fleet, "test_retry_after_a_client_timeout")
    assert outcomes(by_case, "transport.retry_after_timeout") == {"pass" if passes else "fail"}
    first, retry = exchanges(tmp_path)
    assert "Timeout" in first["transport_error"]
    assert retry["request_headers"]["x-stainless-retry-count"] == "1"
    assert code == (0 if passes else 1)


# A slow deployment's shape at a fiftieth of the time: a 60 s next-request budget and its declared
# 5 tokens/s, with a retry that generates 1200 tokens at 20 tokens/s. The whole retry may take
# 60 s plus the 240 s its output takes at the declared rate, so only its start can show a
# retry held behind the abandoned generation.
SCALE = 50
NEXT_REQUEST_S = 60 / SCALE
RETRY_BUDGET = {"next_request_s": NEXT_REQUEST_S, "min_tokens_per_s": 5 * SCALE}


def run_retry(tmp_path, generation, budget):
    fake = {"slow_first_s": 2.0, "retry_generation": generation}
    with FakeFleet(transport=fake) as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_retry_after_a_client_timeout",
            transport={"stream_budget": budget},
        )
    (record,) = by_case["transport.retry_after_timeout"]
    return code, record, {check["name"]: check for check in record["checks"]}


@pytest.mark.parametrize("held_s,passes", [(0.0, True), (90 / SCALE, False)])
def test_retry_after_timeout_times_the_retry_start(tmp_path, held_s, passes):
    generation = {"tokens": 1200, "tokens_per_s": 20 * SCALE, "held_s": held_s}
    code, record, checks = run_retry(tmp_path, generation, RETRY_BUDGET)
    assert record["outcome"] == ("pass" if passes else "fail")
    assert checks["retry_first_output_s"]["ok"] is passes
    if passes:
        parts = checks["retry_bound_s"]["observed"]
        assert parts["completion_tokens"] == 1200
        assert parts["bound_s"] == round(NEXT_REQUEST_S + 1200 / (5 * SCALE), 3)
        assert checks["retry_elapsed_s"]["ok"] is True
    assert code == (0 if passes else 1)
    retry = exchanges(tmp_path)[-1]
    assert retry["request_headers"]["x-stainless-retry-count"] == "1"
    assert retry["request_body"]["stream"] is True


def test_retry_after_timeout_reads_the_retry_stream_for_timing_only(tmp_path):
    # LiteLLM's usage frame repeats the finished choice (#213). The streaming suites report
    # that; here it must not replace the retry's timing verdict.
    generation = {"tokens": 1200, "tokens_per_s": 20 * SCALE, "usage_choice": True}
    code, record, checks = run_retry(tmp_path, generation, RETRY_BUDGET)
    assert record["outcome"] == "pass", record
    assert checks["retry_first_output_s"]["ok"] is True
    assert checks["retry_bound_s"]["observed"]["completion_tokens"] == 1200
    assert checks["retry_elapsed_s"]["ok"] is True
    assert "choice_index" not in checks
    assert code == 0


def test_retry_after_timeout_holds_the_retry_to_the_declared_rate(tmp_path):
    # Served promptly, but 300 tokens at 2 tokens/s take 150 s: above 60 s plus 300 tokens at
    # the declared 5 tokens/s.
    generation = {"tokens": 300, "tokens_per_s": 2 * SCALE}
    code, record, checks = run_retry(tmp_path, generation, RETRY_BUDGET)
    assert checks["retry_first_output_s"]["ok"] is True
    assert checks["retry_elapsed_s"]["ok"] is False
    assert record["outcome"] == "fail"
    assert code == 1


def test_retry_after_timeout_without_a_rate_bounds_only_the_start(tmp_path):
    # 1500 tokens at 20 tokens/s take 75 s, longer than the 60 s budget for the retry's start.
    generation = {"tokens": 1500, "tokens_per_s": 20 * SCALE}
    code, record, checks = run_retry(tmp_path, generation, {"next_request_s": NEXT_REQUEST_S})
    assert checks["retry_first_output_s"]["ok"] is True
    assert "retry_elapsed_s" not in checks
    # The skipped total bound is recorded, so the verdict does not read as one that applied it.
    assert checks["retry_bound_s"]["observed"] == "not applied: no min_tokens_per_s declared"
    assert record["outcome"] == "pass"
    assert code == 0


def test_retry_after_timeout_skips_when_nothing_timed_out(tmp_path):
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, "test_retry_after_a_client_timeout")
    (record,) = by_case["transport.retry_after_timeout"]
    assert record["outcome"] == "skip" and record["skip_class"] == "requires_load"
    assert code == 0


@pytest.mark.parametrize("defects,passes", [(set(), True), ({"rate_limit_garbled"}, False)])
def test_rate_limit_header_values(tmp_path, defects, passes):
    headers = {"x-ratelimit-remaining-requests": "99", "x-ratelimit-reset-requests": "6m0s"}
    with FakeFleet(extra_response_headers=headers, transport={"defects": defects}) as fleet:
        profile = declare(tmp_path, fleet)
        raw = yaml.safe_load(profile.read_text())
        for name in headers:
            raw["headers"][name] = {"fake": "promised"}
        profile.write_text(yaml.safe_dump(raw))
        from tests.test_suites_against_fake import run_suites

        code, records = run_suites(profile, tmp_path / "run", "-k", "test_rate_limit_header_values")
    assert {r["outcome"] for r in records} == {"pass" if passes else "fail"}
    assert code == (0 if passes else 1)


SECRET = "llmdv-transport-test-secret"


def verify_with_auth(tmp_path, fleet, select, transport=None):
    profile = declare(tmp_path, fleet, transport=transport)
    raw = yaml.safe_load(profile.read_text())
    raw["ingresses"][0]["auth"] = {"type": "bearer", "key_env": "LLMDV_TRANSPORT_KEY"}
    profile.write_text(yaml.safe_dump(raw))
    out = tmp_path / "run"
    result = run_llmdv(
        "verify", "--profile", str(profile), "--local-output", str(out), "--pod-logs", "off",
        "--skip-preflight", "-k", select, env={"LLMDV_TRANSPORT_KEY": SECRET},
    )  # fmt: skip
    records = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert SECRET not in (out / "exchanges.jsonl").read_text()
    return result.returncode, {r["case_id"]: r for r in records}


@pytest.mark.parametrize("defects,passes", [(set(), True), ({"auth_500"}, False)])
def test_malformed_credentials(tmp_path, defects, passes):
    with FakeFleet(
        auth_header=("Authorization", f"Bearer {SECRET}"), transport={"defects": defects}
    ) as fleet:
        code, by_case = verify_with_auth(tmp_path, fleet, "test_malformed_credentials")
    assert by_case["headers.malformed_credentials"]["outcome"] == ("pass" if passes else "fail")
    assert code == (0 if passes else 1)


@pytest.mark.parametrize(
    "accepted,declared,passes",
    [
        (["x-api-key"], ["x-api-key"], True),
        ([], [], True),
        (["x-api-key"], [], False),
        ([], ["api-key"], False),
    ],
)
def test_alternate_credentials(tmp_path, accepted, declared, passes):
    with FakeFleet(
        auth_header=("Authorization", f"Bearer {SECRET}"),
        transport={"credential_headers": accepted},
    ) as fleet:
        code, by_case = verify_with_auth(
            tmp_path,
            fleet,
            "test_alternate_credential_headers",
            transport={"credential_headers": declared},
        )
    assert by_case["headers.alternate_credentials"]["outcome"] == ("pass" if passes else "fail")
    assert code == (0 if passes else 1)


@pytest.mark.parametrize("defects,passes", [(set(), True), ({"rejects_authorization"}, False)])
def test_unauthenticated_ingress_ignores_credentials(tmp_path, defects, passes):
    with FakeFleet(transport={"defects": defects}) as fleet:
        code, by_case = run(tmp_path, fleet, "test_unauthenticated_ingress_ignores")
    assert outcomes(by_case, "headers.credentials_ignored") == {"pass" if passes else "fail"}
    assert code == (0 if passes else 1)
