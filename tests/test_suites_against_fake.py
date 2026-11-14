"""Run the real suites against the fake fleet, with no cluster involved.

This is what makes the suite logic testable in CI, and it is also where the negative cases
live: a fleet that silently falls back to another model, and one that reports abort on an HTTP
200. Those are the two failures the suite exists to catch, so they are exercised against a
server that genuinely commits them rather than trusted to a code reading.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
from dataclasses import FrozenInstanceError
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from llmdv.cli.verify_cmd import _plugin_is_registered
from tests.fakes import FakeFleet, FakeModelDiscoveryConfig, FakeResponsesConfig, profile_yaml

SRC = Path(__file__).resolve().parents[1] / "src"
SUITES = SRC / "llmdv" / "suites"


def run_suites(
    profile_path: Path, run_dir: Path, *extra: str, env: dict[str, str] | None = None
) -> tuple[int, list[dict]]:
    """Invoke pytest on the shipped suites in a subprocess, exactly as `llmdv verify` does."""
    result, records = run_pytest(SUITES, profile_path, run_dir, *extra, env=env)
    return result.returncode, records


def run_pytest(
    suites: Path,
    profile_path: Path,
    run_dir: Path,
    *extra: str,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, list[dict]]:
    """Invoke pytest with the llmdv plugin on any suite directory, returning its output too.

    The subprocess gets a fixed environment, so a variable a profile names (`base_url_env`) or
    a proxy (`HTTP_PROXY`) reaches it only through `env`.

    The `-p llmdv.plugin` guard is the same one the CLI uses, and it is load-bearing: once
    the package is installed (even editable, which leaves a discoverable llmdv.egg-info on
    the path)
    pytest auto-loads the plugin from its pytest11 entry point, and loading it again aborts the
    session with "Plugin already registered under a different name".
    """
    argv = [
        sys.executable,
        "-m",
        "pytest",
        str(suites),
        "-q",
        "--no-header",
        "--llmdv-profile",
        str(profile_path),
        "--llmdv-run-dir",
        str(run_dir),
        *extra,
    ]
    if not _plugin_is_registered():
        argv[3:3] = ["-p", "llmdv.plugin"]
    result = subprocess.run(  # noqa: S603 - fixed interpreter and args
        argv,
        capture_output=True,
        text=True,
        cwd=SRC.parent,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin:/usr/local/bin", **(env or {})},
    )
    results_file = run_dir / "results.jsonl"
    records = (
        [json.loads(line) for line in results_file.read_text().splitlines() if line.strip()]
        if results_file.is_file()
        else []
    )
    return result, records


def write_profile(tmp_path: Path, fleet: FakeFleet, **kw) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "fake.yaml"
    path.write_text(profile_yaml(fleet.base_url, fleet.model, **kw))
    return path


def outcomes(records: list[dict]) -> dict[str, str]:
    return {r["case_id"]: r["outcome"] for r in records if r.get("case_id")}


@pytest.mark.parametrize(
    ("config", "field"),
    [
        (FakeResponsesConfig(), "feature_mode"),
        (FakeModelDiscoveryConfig(), "listing_defect"),
    ],
)
def test_responses_coverage_fake_configs_are_immutable(config, field):
    with pytest.raises(FrozenInstanceError):
        setattr(config, field, "changed")


def test_healthy_fake_fleet_passes_every_case(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run")

    assert records, "no results were recorded"
    by_case = outcomes(records)
    assert by_case.get("openai_chat.basic_non_streaming") == "pass"
    assert by_case.get("openai_completions.basic_non_streaming") == "pass"
    assert by_case.get("discovery.models_lists_declared_model") == "pass"
    assert by_case.get("discovery.unknown_model_no_fallback") == "pass"
    assert code == 0, f"expected a clean run, got {code}: {by_case}"


def test_silent_fallback_to_another_model_is_caught(tmp_path):
    """The failure no status code reveals: a generation served for a nonexistent model."""
    with FakeFleet(fall_back_on_unknown_model=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(
            profile, tmp_path / "run", "-k", "unknown_model_rejected_without_fallback"
        )

    assert code != 0
    assert outcomes(records)["discovery.unknown_model_no_fallback"] == "fail"
    record = next(r for r in records if r["case_id"] == "discovery.unknown_model_no_fallback")
    assert "served by some other model" in (record["reason"] or "")


def test_abort_finish_reason_on_http_200_is_caught(tmp_path):
    """A draining replica reports this; a status-only check would score it as a success."""
    with FakeFleet(abort_finish_reason=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_chat_basic and not sdk")

    assert code != 0
    record = next(r for r in records if r["case_id"] == "openai_chat.basic_non_streaming")
    assert record["outcome"] == "fail"
    assert record["http_status"] == 200, "the point is that the status was fine"
    assert "abandoned" in (record["reason"] or "")


def test_non_json_http_200_fails_both_smoke_cases_with_status_evidence(tmp_path):
    with FakeFleet(non_json_success=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(
            profile,
            tmp_path / "run",
            "-k",
            "(test_chat_basic or test_completions_basic) and not sdk",
        )

    assert code == 1
    assert {r["case_id"] for r in records} == {
        "openai_chat.basic_non_streaming",
        "openai_completions.basic_non_streaming",
    }
    for record in records:
        assert record["outcome"] == "fail"
        assert "expected a JSON response object" in record["reason"]
        assert any(c["name"] == "http_status" and c["observed"] == 200 for c in record["checks"])


def test_stale_profile_declaring_an_absent_surface_fails(tmp_path):
    """Declared present, actually absent -> the surface regressed."""
    with FakeFleet(serve_responses=False) as fleet:
        profile = write_profile(tmp_path, fleet, responses=True)
        code, records = run_suites(profile, tmp_path / "run", "-k", "declared_surfaces_exist")

    assert code != 0
    assert outcomes(records)["contract.declared_surfaces_exist"] == "fail"


def test_stale_profile_omitting_a_served_surface_fails(tmp_path):
    """Declared absent, actually present -> coverage is silently missing."""
    with FakeFleet(serve_responses=True) as fleet:
        profile = write_profile(tmp_path, fleet, responses=False)
        code, records = run_suites(profile, tmp_path / "run", "-k", "undeclared_surfaces")

    assert code != 0
    assert outcomes(records)["contract.undeclared_surfaces_absent"] == "fail"


def test_evidence_bundle_is_written_for_a_failure(tmp_path):
    with FakeFleet(abort_finish_reason=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        run_suites(profile, run_dir, "-k", "test_chat_basic and not sdk")

    # Bundles are nested one level under the ingress that produced them, so `ls failures/`
    # names the layers that broke before any case name is read.
    layers = sorted(d.name for d in (run_dir / "failures").iterdir() if d.is_dir())
    assert layers == ["fake"], f"expected the bundle under its ingress, found {layers}"
    bundles = sorted((run_dir / "failures" / "fake").iterdir())
    assert bundles, "a failure produced no evidence bundle"
    bundle = bundles[0]
    for name in ("request.json", "response.json", "meta.json", "repro.sh", "assertion.txt"):
        assert (bundle / name).is_file(), f"{name} missing from the bundle"
    assert (bundle / "repro.sh").read_text().startswith("#!/usr/bin/env bash")
    assert json.loads((bundle / "response.json").read_text())["status"] == 200


def test_unobservable_dispositions_skip_with_the_right_class(tmp_path):
    """A header llmdv cannot verify from outside must skip, with the missing instrument named.

    `rewritten` is what LiteLLM's flow-control hook does to x-llm-d-inference-objective, and
    `consumed` is what the EPP does. Neither is visible to a client, so passing them would
    assert something the evidence does not support. These are the rows that keep a compatibility
    matrix honest.
    """
    with FakeFleet() as fleet:
        profile = tmp_path / "dispositions.yaml"
        profile.write_text(
            textwrap.dedent(f"""\
            schema: llmdv.profile/1
            name: dispositions
            model: {{public_id: {fleet.model}}}
            ingresses:
              - id: fake
                kind: vllm
                base_url: {fleet.base_url}
                health_path: /health
                surfaces: [chat_completions]
            headers:
              x-llm-d-inference-objective: {{fake: rewritten}}
              x-llm-d-inference-fairness-id: {{fake: consumed}}
              x-request-id: {{fake: ignored}}
            """)
        )
        code, records = run_suites(profile, tmp_path / "run", "-k", "header_disposition")

    assert code == 0, "skips are not failures"
    by_header = {
        r["nodeid"].split("[")[-1].rstrip("]"): (r["outcome"], r["skip_class"], r["reason"])
        for r in records
        if r["case_id"] == "contract.header_disposition"
    }

    outcome, skip_class, reason = by_header["x-llm-d-inference-objective@fake"]
    assert (outcome, skip_class) == ("skip", "requires_instrumentation")
    assert "instrumented_backend" in reason

    outcome, skip_class, _ = by_header["x-llm-d-inference-fairness-id@fake"]
    assert (outcome, skip_class) == ("skip", "requires_load")

    # The observable one is actually checked rather than skipped.
    assert by_header["x-request-id@fake"][0] == "pass"


def test_every_case_records_checks_and_exchanges(tmp_path):
    """A verdict must be backed by evidence, for passes as much as for failures.

    Without this, "22 passed" is a claim with nothing behind it. The run has to leave both the
    observations the assertions made and the HTTP traffic that produced them -- including for
    the cases that go through the pinned SDK, which builds its own transport.
    """
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        code, records = run_suites(profile, run_dir, "--llmdv-evidence", "all")

    assert code == 0
    unevidenced = [r["case_id"] for r in records if r["outcome"] == "pass" and not r["checks"]]
    assert not unevidenced, f"passing cases with no recorded checks: {unevidenced}"

    exchanges_file = run_dir / "exchanges.jsonl"
    assert exchanges_file.is_file(), "no exchanges.jsonl was written"
    exchanges = [json.loads(line) for line in exchanges_file.read_text().splitlines() if line]
    assert exchanges, "exchanges.jsonl is empty"

    # The SDK-backed cases build their own transport; they must still leave a record.
    sdk_cases = {e["case_id"] for e in exchanges if "openai_sdk" in (e["case_id"] or "")}
    assert sdk_cases, "the pinned-SDK cases left no exchange evidence"

    # Every exchange carries enough to audit it.
    for exchange in exchanges:
        assert exchange["method"] and exchange["url"]
        assert exchange["status"] is not None or exchange["transport_error"]
        assert exchange["ingress_kind"] == "vllm"
        assert exchange["request_headers"]["x-llmdv-case-id"] == exchange["case_id"]

    assert {e["case_id"] for e in exchanges if "streaming" in e["case_id"]}
    assert {e["case_id"] for e in exchanges if "sdk" in e["case_id"]}


def test_evidence_all_writes_a_bundle_per_case_including_passes(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        _, records = run_suites(profile, run_dir, "--llmdv-evidence", "all")

    # One directory per case, under the ingress that ran it, with the variant in the name so
    # parametrised cases do not overwrite one another.
    layers = [d for d in (run_dir / "cases").iterdir() if d.is_dir()]
    assert [d.name for d in layers] == ["fake"]
    bundles = sorted(b for layer in layers for b in layer.iterdir())
    assert len(bundles) == len(records), (
        f"{len(records)} cases produced {len(bundles)} bundles -- some overwrote each other"
    )
    verdict = (bundles[0] / "verdict.txt").read_text()
    assert "outcome" in verdict and "checks" in verdict and "exchanges" in verdict


def test_default_evidence_mode_writes_bundles_only_for_failures(tmp_path):
    with FakeFleet(abort_finish_reason=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        run_suites(profile, run_dir)

    assert (run_dir / "failures").is_dir()
    assert not (run_dir / "cases").exists(), "passes should not write bundles by default"


def test_a_failing_case_records_a_failing_check(tmp_path):
    """Evidence must agree with the verdict.

    A bundle reading "outcome FAIL" while every check reads "ok" is worse than no evidence: it
    invites the reader to conclude the failure was spurious. The deciding assertion has to
    record the observation that caused it.
    """
    with FakeFleet(abort_finish_reason=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        _, records = run_suites(profile, run_dir, "-k", "test_chat_basic and not sdk")

    failed = [r for r in records if r["outcome"] == "fail"]
    assert failed, "expected a failure"
    for record in failed:
        assert any(not c["ok"] for c in record["checks"]), (
            f"{record['case_id']} failed but every recorded check passed"
        )


def test_a_selection_that_matches_nothing_is_not_reported_as_a_pass(tmp_path):
    """Zero cases is not a green run.

    "PASSED | 0 cases" over an empty selection is the most dangerous output this tool could
    produce: in CI it reads as coverage. pytest's own exit code 5 is mapped to a usage error
    rather than to a fleet verdict.
    """
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "no_such_case_anywhere")

    assert records == []
    assert code == 5, "pytest should report nothing-collected, not success"


# --------------------------------------------------------------------- streaming


def test_a_healthy_stream_passes(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "streaming")

    assert code == 0, f"expected a clean streaming run: {outcomes(records)}"
    by_case = outcomes(records)
    assert by_case.get("openai_chat.streaming_sse") == "pass"
    assert by_case.get("openai_chat.streaming_include_usage") == "pass"


def test_chat_streaming_rejects_valid_frames_with_a_non_sse_content_type(tmp_path):
    with FakeFleet(chat_stream_content_type="application/json") as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(
            profile, tmp_path / "run", "-k", "test_streaming_sse or test_streaming_include_usage"
        )

    assert code == 1
    assert {r["case_id"] for r in records} == {
        "openai_chat.streaming_sse",
        "openai_chat.streaming_include_usage",
    }
    for record in records:
        assert record["outcome"] == "fail" and record["http_status"] == 200
        assert any(c["name"] == "stream_content_type" and not c["ok"] for c in record["checks"])


def test_a_stream_without_a_terminal_sentinel_is_caught(tmp_path):
    """A client reading to [DONE] waits for a frame that never arrives."""
    with FakeFleet(omit_done=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "streaming_sse")

    assert code != 0
    record = next(r for r in records if r["case_id"] == "openai_chat.streaming_sse")
    assert record["outcome"] == "fail"
    assert "[DONE]" in (record["reason"] or "")
    assert any(c["name"] == "terminal_done" and not c["ok"] for c in record["checks"])


def test_usage_emitted_without_the_flag_is_caught(tmp_path):
    """A client that stops at the first choice-less frame would truncate its content."""
    with FakeFleet(usage_without_flag=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "streaming_include_usage")

    assert code != 0
    record = next(r for r in records if r["case_id"] == "openai_chat.streaming_include_usage")
    assert record["outcome"] == "fail"
    assert any(c["name"] == "usage_frames_without_flag" and not c["ok"] for c in record["checks"])


def test_usage_before_the_finish_reason_is_caught(tmp_path):
    """A client that stops at the finish reason would never see the usage."""
    with FakeFleet(usage_before_finish=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "streaming_include_usage")

    assert code != 0
    record = next(r for r in records if r["case_id"] == "openai_chat.streaming_include_usage")
    assert record["outcome"] == "fail"


def test_more_than_one_finish_reason_is_caught(tmp_path):
    with FakeFleet(two_finish_reasons=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "streaming_sse")

    assert code != 0
    record = next(r for r in records if r["case_id"] == "openai_chat.streaming_sse")
    assert any(c["name"] == "finish_reason_frames" and not c["ok"] for c in record["checks"])


def test_a_streamed_case_writes_a_transcript(tmp_path):
    """The transcript is the evidence that the frames were what the assertions claim."""
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        run_suites(profile, run_dir, "-k", "streaming_sse", "--llmdv-evidence", "all")

    # Bundles sit under their ingress, so the glob goes one level deeper than the bucket.
    bundles = sorted((run_dir / "cases").glob("*/*streaming_sse*"))
    assert bundles, "no bundle for the streamed case"
    transcript = (bundles[0] / "transcript.sse").read_text()
    assert transcript.startswith("data: ")
    assert "[DONE]" in transcript


def test_transcript_cap_truncates_from_the_middle_and_records_it(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        _, records = run_suites(
            profile,
            run_dir,
            "-k",
            "streaming_sse",
            "--llmdv-evidence",
            "all",
            "--llmdv-transcript-cap",
            "300",
        )

    # Bundles sit under their ingress, so the glob goes one level deeper than the bucket.
    bundles = sorted((run_dir / "cases").glob("*/*streaming_sse*"))
    transcript = (bundles[0] / "transcript.sse").read_text()
    assert "bytes dropped from the middle" in transcript
    assert "truncated for storage" in transcript

    exchanges = [
        json.loads(line) for line in (run_dir / "exchanges.jsonl").read_text().splitlines() if line
    ]
    truncated = [e for e in exchanges if e.get("transcript_truncated")]
    assert truncated, "truncation was not recorded in exchanges.jsonl"
    assert truncated[0]["transcript_bytes"] > 300
    assert truncated[0]["transcript"]["offload"]["status"] == "not_configured"
    meta = json.loads((bundles[0] / "meta.json").read_text())
    assert "transcript.sse" in meta["artifacts"]
    assert meta["partial_evidence"] is True
    record = next(r for r in records if r["case_id"] == "openai_chat.streaming_sse")
    assert record["partial_evidence"] is True


def test_an_offloaded_transcript_is_whole_and_referenced(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        code, records = run_suites(
            profile,
            run_dir,
            "-k",
            "streaming_sse",
            "--llmdv-evidence",
            "all",
            "--llmdv-transcript-cap",
            "300",
            "--llmdv-artifact-sink",
            f"file://{tmp_path / 'sink'}",
        )

    assert code == 0
    record = next(r for r in records if r["case_id"] == "openai_chat.streaming_sse")
    assert record["partial_evidence"] is False
    bundle = next((run_dir / "cases").glob("*/*streaming_sse*"))
    local = (bundle / "transcript.sse").read_text()
    assert len(local.split("\n", 1)[1].encode()) <= 350
    assert "file://" in local
    exchange = next(
        json.loads(line)
        for line in (run_dir / "exchanges.jsonl").read_text().splitlines()
        if json.loads(line).get("transcript")
    )
    ref = exchange["transcript"]
    stored = next((tmp_path / "sink").rglob("*.sse"))
    assert "[DONE]" in stored.read_text()
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == ref["sha256"]
    assert len(stored.read_bytes()) == ref["bytes"] == exchange["transcript_bytes"]


def test_multiple_streams_reference_the_transcript_written_to_the_bundle(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        code, _ = run_suites(
            profile,
            run_dir,
            "-k",
            "streaming_include_usage",
            "--llmdv-evidence",
            "all",
            "--llmdv-transcript-cap",
            "300",
            "--llmdv-artifact-sink",
            f"file://{tmp_path / 'sink'}",
        )

    assert code == 0
    bundle = next((run_dir / "cases").glob("*/*streaming_include_usage*"))
    exchanges = [
        json.loads(line) for line in (run_dir / "exchanges.jsonl").read_text().splitlines() if line
    ]
    refs = [exchange["transcript"] for exchange in exchanges if exchange.get("transcript")]
    assert len(refs) >= 2
    meta = json.loads((bundle / "meta.json").read_text())
    assert meta["artifacts"]["transcript.sse"] == refs[0]
    assert refs[0]["sha256"] != refs[-1]["sha256"]
    assert refs[0]["offload"]["uri"] in (bundle / "transcript.sse").read_text()
    stored = Path(urlsplit(refs[0]["offload"]["uri"]).path)
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == refs[0]["sha256"]


def test_a_failed_artifact_upload_keeps_the_passing_case_and_marks_partial(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        sink = tmp_path / "not-a-directory"
        sink.write_text("file")
        code, records = run_suites(
            profile,
            tmp_path / "run",
            "-k",
            "streaming_sse",
            "--llmdv-evidence",
            "all",
            "--llmdv-transcript-cap",
            "300",
            "--llmdv-artifact-sink",
            f"file://{sink}",
        )

    assert code == 0
    record = next(r for r in records if r["case_id"] == "openai_chat.streaming_sse")
    assert record["partial_evidence"] is True
    exchange = next(
        json.loads(line)
        for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
        if json.loads(line).get("transcript")
    )
    assert exchange["transcript"]["offload"]["status"] == "failed"


# ----------------------------------------------------------------- unhappy paths


def test_unhappy_paths_pass_against_a_fleet_that_fails_correctly(tmp_path):
    """Every bad request is refused with a status the profile declares and a parseable body."""
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "unhappy")

    by_case = outcomes(records)
    assert by_case, "no unhappy-path cases ran"
    failed = {c: o for c, o in by_case.items() if o == "fail"}
    assert not failed, f"a correctly-failing fleet produced failures: {failed}"
    # Not authenticating is a declared gap, not a pass.
    assert by_case["unhappy.bad_credentials"] == "skip"
    assert code == 0


def test_silent_truncation_of_an_overlong_prompt_is_caught(tmp_path):
    """The dangerous outcome: a 200 and an answer drawn from a prompt the client never sent."""
    with FakeFleet(truncate_overflow=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        _, records = run_suites(profile, tmp_path / "run", "-k", "context_overflow")

    assert outcomes(records).get("unhappy.context_overflow") == "fail"


def test_an_accepted_negative_token_limit_is_caught(tmp_path):
    with FakeFleet(ignore_bad_max_tokens=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        _, records = run_suites(profile, tmp_path / "run", "-k", "invalid_token_limit")

    assert outcomes(records).get("unhappy.invalid_token_limit") == "fail"


def test_an_error_reported_inside_a_promised_stream_is_caught(tmp_path):
    """A 200 stream that then carries the validation failure in a frame."""
    with FakeFleet(stream_before_validation=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        _, records = run_suites(profile, tmp_path / "run", "-k", "streaming_validation")

    assert outcomes(records).get("unhappy.streaming_validation") == "fail"


def test_completions_usage_without_request_is_caught(tmp_path):
    with FakeFleet(usage_without_flag=True) as fleet:
        profile = write_profile(tmp_path, fleet, surfaces=("completions",), responses=False)
        _, records = run_suites(
            profile, tmp_path / "run", "-k", "test_completions_stream_lifecycle"
        )

    completions = [r for r in records if r["surface"] == "completions"]
    failed = [r for r in completions if r["outcome"] == "fail"]
    assert failed and any(
        c["name"] == "usage_without_request" and not c["ok"] for c in failed[0]["checks"]
    )


def test_an_undeclared_unstructured_error_body_is_caught(tmp_path):
    """Prose where an envelope belongs fails unless the ingress declares the deviation."""
    with FakeFleet(plain_text_errors=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        _, records = run_suites(profile, tmp_path / "run", "-k", "malformed_json")

    assert outcomes(records).get("unhappy.malformed_json") == "fail"


def test_a_declared_unstructured_error_body_is_accepted_and_still_falsifiable(tmp_path):
    """Declaring the deviation makes it pass -- and makes its disappearance fail.

    Both directions matter. Without the second half a stale declaration would hide a layer that
    had since been fixed, and the profile would quietly stop describing the fleet.
    """
    declared = ("malformed_json",)
    with FakeFleet(plain_text_errors=True) as fleet:
        profile = write_profile(tmp_path, fleet, unstructured_errors=declared)
        _, records = run_suites(profile, tmp_path / "run", "-k", "malformed_json")
    assert outcomes(records).get("unhappy.malformed_json") == "pass"

    with FakeFleet() as fleet:  # the layer now answers with an envelope after all
        profile = write_profile(tmp_path / "stale", fleet, unstructured_errors=declared)
        _, records = run_suites(profile, tmp_path / "stale-run", "-k", "malformed_json")
    assert outcomes(records).get("unhappy.malformed_json") == "fail"


# ------------------------------------------------------------------- the surface axis

# A second, prompt-shaped surface. The fake validates /v1/completions as `prompt`-shaped and
# ignores unknown fields, as an OpenAI-compatible server does -- so a chat-shaped bad request
# sent there comes back 200 and fails as a fleet defect. Passing on both surfaces is what shows
# each case sent each surface its own shape.


def test_generic_cases_run_once_per_surface_in_that_surfaces_shape(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        code, records = run_suites(profile, run_dir, "-k", "unhappy or discovery")

    by_row = {(r["case_id"], r["surface"]): r["outcome"] for r in records}
    for case in (
        "unhappy.malformed_json",
        "unhappy.missing_required_field",
        "unhappy.wrong_field_type",
        "unhappy.invalid_token_limit",
        "unhappy.context_overflow",
        "unhappy.streaming_validation",
        "unhappy.wrong_method",
        "discovery.unknown_model_no_fallback",
    ):
        assert by_row.get((case, "chat_completions")) == "pass", case
        assert by_row.get((case, "completions")) == "pass", case
        assert by_row.get((case, "messages")) == "pass", case
        assert by_row.get((case, "responses")) == "pass", case
    assert code == 0, f"failures: {[k for k, v in by_row.items() if v != 'pass']}"

    lines = (run_dir / "exchanges.jsonl").read_text().splitlines()
    exchanges = [json.loads(line) for line in lines]
    sent = next(
        e
        for e in exchanges
        if e["case_id"] == "unhappy.wrong_field_type" and e["surface"] == "completions"
    )
    body = sent["request_body"]
    assert "messages" not in body and not isinstance(body["prompt"], str), body
    sent = next(
        e
        for e in exchanges
        if e["case_id"] == "unhappy.invalid_token_limit" and e["surface"] == "responses"
    )
    body = sent["request_body"]
    assert isinstance(body["input"], str) and body["max_output_tokens"] < 0, body
    assert "messages" not in body and "max_tokens" not in body, body


def test_surface_required_headers_travel_without_the_case_asking(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(
            profile,
            tmp_path / "run",
            "-p",
            "tests.surface_headers",
            "--llmdv-surface",
            "completions",
            "-k",
            "missing_required_field",
        )
        ((_, path, headers),) = fleet.seen

    assert code == 0
    assert records[0]["outcome"] == "pass"
    assert path == "/v1/completions"
    assert headers["x-llmdv-surface"] == "completions"


def test_a_case_needing_a_feature_runs_only_where_the_surface_has_it(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        _, records = run_suites(profile, tmp_path / "run", "-k", "invalid_tool")

    surfaces = {(r["case_id"], r["surface"]) for r in records}
    assert surfaces == {
        ("unhappy.invalid_tool_schema", "chat_completions"),
        ("unhappy.invalid_tool_choice", "chat_completions"),
        ("unhappy.invalid_tool_schema", "messages"),
        ("unhappy.invalid_tool_choice", "messages"),
        ("unhappy.invalid_tool_schema", "responses"),
        ("unhappy.invalid_tool_choice", "responses"),
    }


def test_a_surface_independent_case_runs_once_per_ingress(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        _, records = run_suites(profile, tmp_path / "run", "-k", "unknown_path or models_lists")

    assert sorted((r["case_id"], r["surface"]) for r in records) == [
        ("discovery.models_lists_declared_model", None),
        ("unhappy.unknown_path", None),
    ]


def test_a_defect_is_caught_on_every_surface(tmp_path):
    with FakeFleet(ignore_bad_max_tokens=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "invalid_token_limit")

    assert code != 0
    assert {r["surface"]: r["outcome"] for r in records} == {
        "chat_completions": "fail",
        "completions": "fail",
        "messages": "fail",
        "responses": "fail",
    }


def test_a_surface_the_ingress_does_not_expose_skips_as_undeclared(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet, surfaces=("chat_completions",), responses=False)
        code, records = run_suites(profile, tmp_path / "run", "-k", "missing_required_field")

    assert code == 0
    assert {r["surface"]: (r["outcome"], r["skip_class"]) for r in records} == {
        "chat_completions": ("pass", None),
        "completions": ("skip", "undeclared"),
        "messages": ("skip", "undeclared"),
        "responses": ("skip", "undeclared"),
    }


def test_the_surface_selector_narrows_generic_cases_to_one_surface(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        _, records = run_suites(
            profile,
            tmp_path / "run",
            "--llmdv-surface",
            "completions",
            "-k",
            "missing_required_field",
        )

    assert [(r["surface"], r["outcome"]) for r in records] == [("completions", "pass")]


def test_a_header_is_carried_by_a_surface_the_ingress_exposes(tmp_path):
    """An ingress without chat must not fail an `ignored` header with a 404 from chat's path."""
    with FakeFleet(serve_chat=False) as fleet:
        profile = write_profile(tmp_path, fleet, surfaces=("completions",), responses=False)
        _, records = run_suites(profile, tmp_path / "run", "-k", "header_disposition")

    (record,) = records
    assert record["outcome"] == "pass", record["reason"]
    assert any(
        c["name"] == "carried_by" and c["observed"] == "completions" for c in record["checks"]
    )


# ------------------------------------------------------ how a case chooses its surfaces


def run_suite_source(tmp_path, fleet, files: dict[str, str], *extra: str):
    """Run a throwaway suite written from `files` with the production surface registry."""
    suite = tmp_path / "suite"
    suite.mkdir(parents=True)
    for name, source in files.items():
        (suite / name).write_text(textwrap.dedent(source))
    profile = write_profile(tmp_path, fleet)
    return run_pytest(suite, profile, tmp_path / "run", *extra)


def test_the_closest_surface_marker_decides(tmp_path):
    files = {
        "test_generic_module.py": """
            import pytest

            pytestmark = pytest.mark.surface_generic


            @pytest.mark.surface("chat_completions")
            @pytest.mark.case_id("probe.pinned_in_generic_module")
            def test_pinned(client, surface, ingress):
                assert surface.name == "chat_completions"
        """,
        "test_pinned_module.py": """
            import pytest

            pytestmark = pytest.mark.surface("chat_completions")


            @pytest.mark.surface_generic
            @pytest.mark.case_id("probe.generic_in_pinned_module")
            def test_generic(client, surface, ingress):
                pass
        """,
    }
    with FakeFleet() as fleet:
        result, records = run_suite_source(tmp_path, fleet, files)

    assert result.returncode == 0, result.stdout
    assert sorted((r["case_id"], r["surface"]) for r in records) == [
        ("probe.generic_in_pinned_module", "chat_completions"),
        ("probe.generic_in_pinned_module", "completions"),
        ("probe.generic_in_pinned_module", "messages"),
        ("probe.generic_in_pinned_module", "responses"),
        ("probe.pinned_in_generic_module", "chat_completions"),
    ]


def test_both_surface_markers_on_one_case_fail_collection(tmp_path):
    """A contradiction is a llmdv defect, reported at collection rather than resolved silently."""
    files = {
        "test_both.py": """
            import pytest


            @pytest.mark.surface("chat_completions")
            @pytest.mark.surface_generic
            @pytest.mark.case_id("probe.both")
            def test_both(client, surface, ingress):
                pass
        """,
    }
    with FakeFleet() as fleet:
        result, records = run_suite_source(tmp_path, fleet, files)

    # pytest's "interrupted by collection errors", which `llmdv verify` maps to exit 4.
    assert result.returncode == 2
    assert "marked both surface(...) and surface_generic" in result.stdout
    assert records == []


def test_an_unknown_surface_feature_fails_collection(tmp_path):
    """A misspelt feature would otherwise match no surface and select nothing."""
    files = {
        "test_typo.py": """
            import pytest


            @pytest.mark.surface_generic("tool")
            @pytest.mark.case_id("probe.typo")
            def test_typo(client, surface, ingress):
                pass
        """,
    }
    with FakeFleet() as fleet:
        result, records = run_suite_source(tmp_path, fleet, files)

    assert result.returncode == 2
    assert "unknown features ['tool']" in result.stdout
    assert records == []


def test_a_feature_no_drivable_surface_has_skips_as_not_implemented(tmp_path):
    """One visible skip row, where an empty parametrisation crashed the reporter."""
    files = {
        # Stands in for a feature declared ahead of the first surface llmdv can drive with it.
        "conftest.py": """
            import llmdv.plugin


            def pytest_configure(config):
                plugin = llmdv.plugin
                plugin.SURFACE_FEATURES = plugin.SURFACE_FEATURES | {"future_feature"}
        """,
        "test_future.py": """
            import pytest


            @pytest.mark.surface_generic("future_feature")
            @pytest.mark.case_id("probe.future")
            def test_future(client, surface, ingress):
                pass
        """,
    }
    with FakeFleet() as fleet:
        result, records = run_suite_source(tmp_path, fleet, files)

    assert result.returncode == 0, result.stdout
    assert [(r["outcome"], r["skip_class"], r["surface"]) for r in records] == [
        ("skip", "not_implemented", None)
    ]
    assert "future_feature" in records[0]["reason"]


def test_ingresses_that_differ_on_responses_each_pass_their_own_declaration(tmp_path):
    """One ingress serves /v1/responses and the other does not, and both are declared so.

    The removed profile-wide `capabilities.responses` could not describe this fleet: either
    value failed one of the two ingresses. The per-ingress `surfaces` lists describe it exactly.
    """
    with FakeFleet(serve_responses=True) as serves, FakeFleet(serve_responses=False) as lacks:
        profile = tmp_path / "mixed.yaml"
        profile.write_text(
            textwrap.dedent(f"""\
            schema: llmdv.profile/1
            name: mixed
            model: {{public_id: {serves.model}}}
            ingresses:
              - id: serves
                kind: vllm
                base_url: {serves.base_url}
                surfaces: [chat_completions, completions, messages, responses]
              - id: lacks
                kind: vllm
                base_url: {lacks.base_url}
                surfaces: [chat_completions, completions, messages]
            """)
        )
        code, records = run_suites(profile, tmp_path / "run", "-m", "contract", "-k", "surfaces")

    by_row = {(r["case_id"], r["ingress"]): r["outcome"] for r in records}
    assert by_row == {
        ("contract.declared_surfaces_exist", "serves"): "pass",
        ("contract.declared_surfaces_exist", "lacks"): "pass",
        ("contract.undeclared_surfaces_absent", "serves"): "pass",
        ("contract.undeclared_surfaces_absent", "lacks"): "pass",
    }
    assert code == 0
