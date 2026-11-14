"""The transport request cases against a fake that serves HTTP correctly, then against defects."""

from __future__ import annotations

import pytest

from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile
from tests.transport_helpers import exchanges, outcomes, run

HTTP_CASES = (
    "test_method_not_allowed or test_content_type_variants or test_accept_does_not_choose "
    "or test_gzip_request_body or test_response_coding or test_chunked_request_body "
    "or test_expect_continue or test_long_header_value or test_header_names_are_case "
    "or test_header_name_with_underscore or test_client_and_sdk_headers "
    "or test_query_string_on_an_api_path or test_path_variant or test_cors_preflight "
    "or test_body_edges"
)


def test_http_cases_pass_against_a_correct_fleet(tmp_path):
    with FakeFleet(transport={"cors": "allowed"}) as fleet:
        code, by_case = run(tmp_path, fleet, HTTP_CASES, transport={"cors_preflight": "allowed"})
    expected = {
        "transport.method_not_allowed",
        "transport.content_type",
        "transport.accept_event_stream",
        "transport.request_gzip",
        "transport.response_encoding",
        "transport.chunked_request",
        "transport.expect_continue",
        "transport.long_header",
        "transport.header_name_case",
        "transport.header_underscore",
        "transport.client_headers",
        "transport.query_string",
        "transport.path_variant",
        "transport.cors_preflight",
        "transport.body_edge",
    }
    assert set(by_case) == expected
    failed = {
        case: outcomes(by_case, case) for case in expected if outcomes(by_case, case) != {"pass"}
    }
    assert not failed, failed
    assert code == 0
    sent = exchanges(tmp_path)
    bodies = [e["request_body"] for e in sent if isinstance(e.get("request_body"), dict)]
    assert any(item.get("content_encoding") == "gzip" for item in bodies)
    assert any("?beta=true" in e["url"] for e in sent)


def test_compressed_responses_decode(tmp_path):
    with FakeFleet(transport={"compress": True}) as fleet:
        code, by_case = run(tmp_path, fleet, "test_response_coding")
    assert outcomes(by_case, "transport.response_encoding") == {"pass"}
    codings = {e["response_headers"].get("content-encoding") for e in exchanges(tmp_path)}
    assert "gzip" in codings
    assert code == 0


def test_accept_event_stream_uses_a_completions_only_ingress(tmp_path):
    with FakeFleet(serve_chat=False) as fleet:
        profile = write_profile(tmp_path, fleet, surfaces=("completions",), responses=False)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_accept_does_not_choose")

    assert code == 0, records
    assert [record["case_id"] for record in records] == ["transport.accept_event_stream"]
    assert records[0]["outcome"] == "pass"
    assert fleet.seen and all(path == "/v1/completions" for _, path, _ in fleet.seen)


@pytest.mark.parametrize(
    "defect,select,case_id",
    [
        ("method_5xx", "test_method_not_allowed", "transport.method_not_allowed"),
        ("no_allow_header", "test_method_not_allowed", "transport.method_not_allowed"),
        ("charset_415", "test_content_type_variants", "transport.content_type"),
        ("content_type_500", "test_content_type_variants", "transport.content_type"),
        ("accept_sse", "test_accept_does_not_choose", "transport.accept_event_stream"),
        ("gzip_500", "test_gzip_request_body", "transport.request_gzip"),
        ("gzip_always", "test_response_coding", "transport.response_encoding"),
        ("chunked_411", "test_chunked_request_body", "transport.chunked_request"),
        ("expect_417", "test_expect_continue", "transport.expect_continue"),
        ("long_header_431", "test_long_header_value", "transport.long_header"),
        ("header_case_sensitive", "test_header_names_are_case", "transport.header_name_case"),
        ("underscore_502", "test_header_name_with_underscore", "transport.header_underscore"),
        ("client_header_400", "test_client_and_sdk_headers", "transport.client_headers"),
        ("query_string_404", "test_query_string_on_an_api_path", "transport.query_string"),
        ("path_variant_500", "test_path_variant", "transport.path_variant"),
        ("malformed_body_500", "test_body_edges", "transport.body_edge"),
        ("trailing_bytes_served", "test_body_edges", "transport.body_edge"),
    ],
)
def test_each_http_defect_fails_its_case(tmp_path, defect, select, case_id):
    with FakeFleet(transport={"defects": {defect}}) as fleet:
        code, by_case = run(tmp_path, fleet, select)
    assert "fail" in outcomes(by_case, case_id), by_case.get(case_id)
    assert code == 1


def test_declared_cors_must_match_the_preflight(tmp_path):
    with FakeFleet() as fleet:
        code, by_case = run(
            tmp_path, fleet, "test_cors_preflight", transport={"cors_preflight": "allowed"}
        )
    assert outcomes(by_case, "transport.cors_preflight") == {"fail"}
    assert code == 1


def test_cors_skips_without_a_declaration(tmp_path):
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, "test_cors_preflight")
    (record,) = by_case["transport.cors_preflight"]
    assert record["outcome"] == "skip" and record["skip_class"] == "undeclared"
    assert code == 0


def test_a_declared_refusal_pins_the_status(tmp_path):
    """The underscore probe passes on the fake's 200 by default, and fails a declared 400."""
    with FakeFleet() as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_header_name_with_underscore",
            error_status={"header_underscore": [400]},
        )
    assert outcomes(by_case, "transport.header_underscore") == {"fail"}
    assert code == 1


REWRITE = "x-llmdv-model-rewrite"


@pytest.mark.parametrize(
    "fake,passes",
    [
        ({"model_rewrite": REWRITE}, True),
        ({"model_rewrite": REWRITE, "defects": {"rewrite_echoes_served"}}, False),
        ({}, False),
    ],
)
def test_model_rewrite_header(tmp_path, fake, passes):
    with FakeFleet(transport=fake) as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_model_rewrite_header",
            transport={"model_rewrite_header": REWRITE},
        )
    assert outcomes(by_case, "transport.model_rewrite_header") == {"pass" if passes else "fail"}
    (sent,) = exchanges(tmp_path)
    assert sent["request_headers"][REWRITE] == fleet.model
    assert sent["request_body"]["model"].startswith("llmdv-alias-")
    assert code == (0 if passes else 1)


LIMIT = 64 * 1024


@pytest.mark.parametrize(
    "fake,passes",
    [
        ({"max_body_bytes": LIMIT}, True),
        ({"max_body_bytes": LIMIT, "defects": {"body_limit_500"}}, False),
        ({}, False),
    ],
)
def test_body_limit(tmp_path, fake, passes):
    with FakeFleet(transport=fake) as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_body_above_the_declared_limit",
            transport={"max_body_bytes": LIMIT},
            error_status={"body_too_large": [413]},
        )
    assert outcomes(by_case, "transport.body_limit") == {"pass" if passes else "fail"}
    (sent,) = exchanges(tmp_path)
    assert sent["request_body"].startswith("<generation body: ")
    assert int(sent["request_body"].split(": ")[1].split()[0]) > LIMIT
    assert code == (0 if passes else 1)
