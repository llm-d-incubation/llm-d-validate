"""The run dashboard: one HTML file per run, built when the run finishes, never a reason to fail.

The dashboard is a reading aid over evidence llmdv already wrote. So the contract is narrow: it is
built from the run directory alone, it carries no credential, and nothing about it can change a
run's exit code or stop the run being published.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import textwrap

import pytest

from llmdv.evidence import dashboard
from llmdv.http import redact
from tests.fakes import FakeFleet, profile_yaml, run_llmdv

PROFILE = textwrap.dedent(
    """
    schema: llmdv.profile/1
    name: dash
    model: {public_id: m}
    ingresses:
      - id: gw
        kind: litellm
        base_url: https://gateway.example
        auth: {type: bearer, key_env: DASH_KEY}
        surfaces: [chat_completions]
    """
)


def _record(nodeid, case_id, outcome, **extra):
    base = {
        "schema": "llmdv.results/1", "run_id": "r1", "nodeid": nodeid, "case_id": case_id,
        "outcome": outcome, "ingress": "gw", "surface": "chat_completions", "variant": None,
        "skip_class": None, "reason": None, "duration_s": 0.1, "http_status": None,
        "served_by": {}, "checks": [], "evidence": None, "ts": "2026-10-12T00:00:00+00:00",
    }  # fmt: skip
    base.update(extra)
    return base


def make_run(tmp_path, *, finished=True, publication=None, reason="AssertionError: boom"):
    run = tmp_path / "run"
    run.mkdir()
    (run / "profile.used.yaml").write_text(PROFILE)
    node_fail = "src/llmdv/suites/x/test_x.py::test_fail[gw-chat_completions]"
    records = [
        _record("src/llmdv/suites/x/test_x.py::test_ok[gw]", "x.ok", "pass",
                checks=[{"name": "http_status", "observed": 200, "expected": "200", "ok": True}]),
        _record(node_fail, "x.fails", "fail", reason=reason, http_status=500,
                checks=[{"name": "error_envelope", "observed": None, "expected": "an envelope",
                         "ok": False}]),
        _record("src/llmdv/suites/x/test_x.py::test_skip[gw]", "x.skips", "skip",
                skip_class="undeclared", reason="Skipped: [undeclared] nope"),
    ]  # fmt: skip
    (run / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    exchange = {
        "run_id": "r1", "nodeid": node_fail, "case_id": "x.fails", "ingress": "gw",
        "surface": "chat_completions", "seq": 0, "method": "POST",
        "url": "https://gateway.example/v1/chat/completions",
        "request_headers": {"Authorization": "***REDACTED***", "Content-Type": "application/json"},
        "request_body": {"model": "m", "messages": []}, "status": 500,
        "response_headers": {"x-litellm-version": "1.97.0"}, "response_body": {"error": "x"},
        "elapsed_s": 0.2, "transport_error": None,
    }  # fmt: skip
    (run / "exchanges.jsonl").write_text(json.dumps(exchange) + "\n")
    outcome = {
        "schema": "llmdv.outcome/1", "llmdv_version": "0", "run_id": "r1",
        "started": "2026-10-12T00:00:00+00:00",
        "finished": "2026-10-12T00:01:00+00:00" if finished else None,
        "profile": {"name": "dash"}, "publication": publication or {},
        "verification": {
            "exit_code": 1 if finished else None, "class": "fleet" if finished else None,
        },
    }  # fmt: skip
    (run / "outcome.json").write_text(json.dumps(outcome))
    if finished:
        (run / "env.json").write_text(json.dumps({"versions": {"python": "3"}}))
    return run


def payload(path):
    """The page's embedded data, decompressed: what a browser renders."""
    text = path.read_text()
    packed = re.search(r'<script id="data"[^>]*>([^<]+)</script>', text).group(1).strip()
    return json.loads(gzip.decompress(base64.b64decode(packed)))


@pytest.fixture(autouse=True)
def _no_secrets():
    redact.reset_secrets()
    yield
    redact.reset_secrets()


def test_a_dashboard_holds_every_test_with_its_evidence_and_hints(tmp_path):
    run = make_run(tmp_path)
    out = dashboard.write(run)
    assert out == run / "dashboard.html"
    data = payload(out)
    assert {t["outcome"] for t in data["tests"]} == {"pass", "fail", "skip"}
    failing = next(t for t in data["tests"] if t["outcome"] == "fail")
    assert failing["exchanges"][0]["status"] == 500
    assert "${DASH_KEY}" in failing["exchanges"][0]["curl"]
    assert "-k 'test_fail[gw-chat_completions]'" in failing["rerun"]
    assert "hints" not in failing  # the page shows only what llmdv recorded
    assert data["clusters"][0]["key"] == "error_envelope"
    assert not data["in_progress"]


def test_the_profile_the_run_was_judged_against_is_embedded_verbatim(tmp_path):
    """Copyable and downloadable from the page, so the run can be reproduced from it alone."""
    data = payload(dashboard.write(make_run(tmp_path)))
    assert data["profile_raw"] == PROFILE
    assert data["profile"]["name"] == "dash"


def test_a_run_without_a_finish_time_is_built_and_marked_partial(tmp_path):
    data = payload(dashboard.write(make_run(tmp_path, finished=False)))
    assert data["in_progress"] is True


def test_a_requested_upload_gets_its_planned_location_and_pull_command(tmp_path):
    """Built before publishing, so the location is the one deliver() will use."""
    run = make_run(tmp_path, publication={"destination": "s3://bucket/prefix"})
    data = payload(dashboard.write(run))
    assert data["publication"]["location"] == "s3://bucket/prefix/dash/r1/"
    assert data["pull"].startswith('aws s3 cp --recursive "s3://bucket/prefix/dash/r1/"')


def test_a_known_credential_in_the_evidence_refuses_the_page(tmp_path):
    """The data is compressed inside the HTML, so a grep proves nothing; the build must check."""
    run = make_run(tmp_path, reason="AssertionError: server echoed sk-live-abcdefghijklmnopqrstu")
    redact.install_secrets({"sk-live-abcdefghijklmnopqrstu"})
    with pytest.raises(dashboard.CredentialFound):
        dashboard.write(run)
    assert not (run / "dashboard.html").exists()


def test_a_credential_shaped_string_refuses_the_page_even_when_unregistered(tmp_path):
    run = make_run(tmp_path, reason="AssertionError: got Bearer abcdefghijklmnopqrstuvwxyz012345")
    with pytest.raises(dashboard.CredentialFound):
        dashboard.write(run)


def _with_exchange_body(run, body):
    path = run / "exchanges.jsonl"
    exchange = json.loads(path.read_text())
    exchange["response_body"] = body
    path.write_text(json.dumps(exchange) + "\n")


@pytest.mark.parametrize(
    "body",
    [
        {"api_key": "plainvalue-0123456789"},
        {"data": [{"Authorization": "token-not-redacted-42"}]},
        {"client_secret": "s3cr3t-value-xyz"},
        {"password": "hunter2-hunter2"},
    ],
)
def test_a_credential_named_field_refuses_the_page_with_nothing_registered(tmp_path, body):
    """Analysing a run fetched from storage registers no secrets and has no key in the
    environment, so the field-name scan is what still catches an unredacted credential."""
    run = make_run(tmp_path)
    _with_exchange_body(run, body)
    with pytest.raises(dashboard.CredentialFound, match="field named"):
        dashboard.write(run)
    assert not (run / "dashboard.html").exists()


@pytest.mark.parametrize(
    "body",
    [
        {"max_tokens": 128, "prompt_cache_key": "session-123456", "usage": {"total_tokens": 9}},
        {"authorization": "***REDACTED***"},
        {"api_key": "${DASH_KEY}"},
        {"token": "Hello", "logprob": -0.1, "top_logprobs": [{"token": " world"}]},
        {"secret": "a model answer about secrets"},
    ],
)
def test_ordinary_and_redacted_fields_do_not_refuse_the_page(tmp_path, body):
    run = make_run(tmp_path)
    _with_exchange_body(run, body)
    assert dashboard.write(run).exists()


def test_a_credential_llmdv_reads_from_the_environment_refuses_the_page(tmp_path, monkeypatch):
    """S3, COS and webhook credentials are scanned when set, not only the profile's key_env."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMIexampleKEY0")
    run = make_run(tmp_path, reason="AssertionError: upload said wJalrXUtnFEMIexampleKEY0")
    with pytest.raises(dashboard.CredentialFound, match="registered credential"):
        dashboard.write(run)


@pytest.mark.parametrize("ingresses", ["{gw: {kind: vllm}}", "5", "[1, two, {id: ok}]"])
def test_an_invalid_ingresses_value_still_builds_a_page(tmp_path, ingresses):
    """An exit-2 run records the invalid profile verbatim; the page must still build from it."""
    run = make_run(tmp_path)
    (run / "profile.used.yaml").write_text(
        f"schema: llmdv.profile/1\nname: dash\ningresses: {ingresses}\n"
    )
    out = dashboard.write(run)
    assert payload(out)["tests"]


def test_best_effort_never_raises(tmp_path, monkeypatch):
    """A broken dashboard must never change a run's exit code or stop it being published."""

    def broken(*args, **kwargs):
        raise RuntimeError("template missing")

    monkeypatch.setattr(dashboard, "write", broken)
    assert dashboard.write_best_effort(make_run(tmp_path)) is None


def test_analyze_command_builds_a_dashboard(tmp_path):
    run = make_run(tmp_path)
    result = run_llmdv("analyze", str(run))
    assert result.returncode == 0, result.stderr
    assert (run / "dashboard.html").exists()
    assert "error_envelope" in result.stdout


def test_analyze_refuses_a_directory_that_is_not_a_run(tmp_path):
    result = run_llmdv("analyze", str(tmp_path))
    assert result.returncode == 2


def test_analyze_reports_a_credential_and_writes_nothing(tmp_path):
    run = make_run(tmp_path, reason="AssertionError: got Bearer abcdefghijklmnopqrstuvwxyz012345")
    result = run_llmdv("analyze", str(run))
    assert result.returncode == 5
    assert not (run / "dashboard.html").exists()


def _verify(tmp_path, fleet, *extra):
    profile = tmp_path / "fake.yaml"
    profile.write_text(profile_yaml(fleet.base_url, fleet.model))
    run_dir = tmp_path / "run"
    result = run_llmdv(
        "verify", "--profile", str(profile), "--local-output", str(run_dir),
        "--pod-logs", "off", "-k", "discovery", *extra,
    )  # fmt: skip
    return result, run_dir


def test_verify_writes_no_dashboard_by_default(tmp_path):
    with FakeFleet() as fleet:
        result, run_dir = _verify(tmp_path, fleet)
    assert result.returncode == 0, result.stderr
    assert not (run_dir / "dashboard.html").exists()


def test_verify_with_analyze_writes_and_publishes_the_dashboard(tmp_path):
    """Built before publishing, so the remote copy carries it and its manifest covers it."""
    archive = tmp_path / "archive"
    with FakeFleet() as fleet:
        result, run_dir = _verify(
            tmp_path, fleet, "--analyze", "--remote-output", f"file://{archive}"
        )
    assert result.returncode == 0, result.stderr
    data = payload(run_dir / "dashboard.html")
    assert data["tests"] and data["exit"]["code"] == 0
    published = next(archive.rglob("dashboard.html"))
    manifest = json.loads((published.parent / "MANIFEST.json").read_text())
    assert "dashboard.html" in {entry["path"] for entry in manifest["files"]}


def test_publish_with_analyze_builds_the_dashboard_for_a_crashed_run(tmp_path):
    run = make_run(tmp_path, finished=False)
    archive = tmp_path / "archive"
    result = run_llmdv(
        "publish", str(run), "--remote-output", f"file://{archive}", "--exit-code", "137",
        "--analyze",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    data = payload(run / "dashboard.html")
    assert data["exit"]["code"] == 137
    assert next(archive.rglob("dashboard.html"))


# ------------------------------------------------------------- other run shapes


MULTI = textwrap.dedent(
    """
    schema: llmdv.profile/1
    name: layers
    model: {public_id: m}
    ingresses:
      - {id: engine, kind: vllm, base_url: "http://localhost:8000", surfaces: [chat_completions]}
      - {id: router, kind: llmd_router, base_url: "http://localhost:8001", surfaces: [messages]}
    """
)


def test_a_multi_ingress_profile_without_auth(tmp_path):
    run = make_run(tmp_path)
    (run / "profile.used.yaml").write_text(MULTI)
    data = payload(dashboard.write(run))
    assert [i["id"] for i in data["profile"]["ingresses"]] == ["engine", "router"]
    failing = next(t for t in data["tests"] if t["outcome"] == "fail")
    assert "${" not in failing["exchanges"][0]["curl"]  # no credential to reference


def test_a_run_killed_before_any_result(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "outcome.json").write_text(json.dumps({"run_id": "early", "verification": {}}))
    data = payload(dashboard.write(run))
    assert data["tests"] == [] and data["in_progress"]


def test_odd_records_and_an_unreadable_profile_still_build(tmp_path):
    run = make_run(tmp_path)
    (run / "profile.used.yaml").write_text("- not\n- a mapping: [")
    with (run / "results.jsonl").open("a") as handle:
        handle.write(json.dumps({"outcome": "fail", "nodeid": "", "checks": None}) + "\n")
        handle.write("{torn line of a killed run\n")
    with (run / "exchanges.jsonl").open("a") as handle:
        handle.write(json.dumps({"nodeid": "", "status": None, "transport_error": "ConnectError",
                                 "response_body": "plain text"}) + "\n")  # fmt: skip
    data = payload(dashboard.write(run))
    assert len(data["tests"]) == 4
    assert data["profile"] == {}


# ------------------------------------------------------------- agent notes


NOTES = {
    "author": "an agent, from the run's evidence",
    "summary": "One gateway defect explains the failure.",
    "groups": {"error_envelope": "LiteLLM stringifies the native error."},
    "cases": {
        "x.fails": "Body is an OpenAI envelope on a Messages route.",
        "x.not_a_case": "This key matches nothing.",
    },
}


def test_agent_notes_are_embedded_labelled_and_matched_to_tests(tmp_path):
    run = make_run(tmp_path)
    out, unmatched = dashboard.write(run, notes=NOTES), dashboard.unmatched_notes(run, NOTES)
    data = payload(out)
    assert data["notes"]["author"] == NOTES["author"]
    assert data["notes"]["summary"].startswith("One gateway")
    failing = next(t for t in data["tests"] if t["outcome"] == "fail")
    assert failing["note"] == NOTES["cases"]["x.fails"]
    assert data["clusters"][0]["note"] == NOTES["groups"]["error_envelope"]
    assert unmatched == ["x.not_a_case"]


def test_without_notes_the_page_carries_none(tmp_path):
    data = payload(dashboard.write(make_run(tmp_path)))
    assert data["notes"] is None
    assert all("note" not in t for t in data["tests"])


def test_a_credential_in_agent_notes_refuses_the_page(tmp_path):
    run = make_run(tmp_path)
    notes = {"author": "a", "cases": {"x.fails": "key was sk-live-abcdefghijklmnopqrstu"}}
    with pytest.raises(dashboard.CredentialFound):
        dashboard.write(run, notes=notes)


def test_analyze_takes_a_notes_file_and_reports_unmatched_keys(tmp_path):
    run = make_run(tmp_path)
    notes = tmp_path / "notes.json"
    notes.write_text(json.dumps(NOTES))
    result = run_llmdv("analyze", str(run), "--notes", str(notes))
    assert result.returncode == 0, result.stderr
    assert payload(run / "dashboard.html")["notes"]["summary"]
    assert "x.not_a_case" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "content",
    ["not json", json.dumps(["a list"]), json.dumps({"cases": {"x": 1}}), json.dumps({"other": 1})],
)
def test_analyze_refuses_a_malformed_notes_file(tmp_path, content):
    run = make_run(tmp_path)
    notes = tmp_path / "notes.json"
    notes.write_text(content)
    result = run_llmdv("analyze", str(run), "--notes", str(notes))
    assert result.returncode == 2
    assert not (run / "dashboard.html").exists()


def test_agent_intents_describe_every_variant_of_a_case(tmp_path):
    """What a test set out to prove, so a reader need not reverse-engineer it from the request."""
    run = make_run(tmp_path)
    notes = {
        "author": "a",
        "intents": {"x.fails": "Sends a chat request and checks errors carry an envelope.",
                    "x.unknown": "Matches nothing."},
    }  # fmt: skip
    data = payload(dashboard.write(run, notes=notes))
    failing = next(t for t in data["tests"] if t["case_id"] == "x.fails")
    assert failing["intent"].startswith("Sends a chat request")
    assert all("intent" not in t for t in data["tests"] if t["case_id"] != "x.fails")
    assert dashboard.unmatched_notes(run, notes) == ["intent: x.unknown"]


def test_intents_must_be_strings_keyed_by_case_id(tmp_path):
    with pytest.raises(ValueError):
        dashboard.validate_notes({"intents": {"x.fails": ["not", "a", "string"]}})


def test_analyze_names_the_cases_still_missing_an_intent(tmp_path):
    run = make_run(tmp_path)
    notes = tmp_path / "notes.json"
    notes.write_text(json.dumps({"intents": {"x.fails": "Checks the error envelope."}}))
    result = run_llmdv("analyze", str(run), "--notes", str(notes))
    assert result.returncode == 0, result.stderr
    assert "2 case(s) have no intent: x.ok, x.skips" in result.stdout + result.stderr


def test_a_mistyped_group_key_is_reported_not_dropped(tmp_path):
    run = make_run(tmp_path)
    notes = {"groups": {"error_envelope": "ok", "error_envlope": "typo"}}
    assert dashboard.unmatched_notes(run, notes) == ["group: error_envlope"]


def test_analyze_prints_every_failure_group_so_notes_can_name_them(tmp_path):
    run = make_run(tmp_path)
    with (run / "results.jsonl").open("a") as handle:
        for i in range(12):
            check = {"name": f"check_{i}", "observed": 1, "ok": False}
            record = _record(f"n{i}", f"y.case{i}", "fail", checks=[check])
            handle.write(json.dumps(record) + "\n")
    result = run_llmdv("analyze", str(run))
    assert result.returncode == 0, result.stderr
    assert all(f"check_{i}" in result.stdout for i in range(12))


def test_the_stack_table_shows_router_headers_and_the_api_base_the_traffic_carried(tmp_path):
    """dashboard.html reads a fixed key list plus `router header: ` keys; the data must keep
    emitting that shape, or the table renders "none seen" for headers that were there."""
    run = make_run(tmp_path)
    exchange = json.loads((run / "exchanges.jsonl").read_text())
    exchange["response_headers"] = {
        "x-litellm-version": "1.97.0",
        "x-litellm-model-api-base": "http://epp.internal:8000",
        "x-llm-d-inference-objective": "default",
        "x-went-into-resp-headers": "true",
    }
    (run / "exchanges.jsonl").write_text(json.dumps(exchange) + "\n")
    page = dashboard.write_best_effort(run)
    [row] = [r for r in payload(page)["stack"] if r["ingress"] == "gw"]
    assert row["found"]["x-litellm-version"] == {"1.97.0": 1}
    assert row["found"]["x-litellm-model-api-base"] == {"http://epp.internal:8000": 1}
    assert row["found"]["router header: x-llm-d-inference-objective"] == {"default": 1}
    assert row["found"]["router header: x-went-into-resp-headers"] == {"true": 1}
