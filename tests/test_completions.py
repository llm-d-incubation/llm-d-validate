"""A completion-only client must detect a failed generation even when HTTP succeeds."""

from __future__ import annotations

import json

import pytest

from llmdv.surfaces import get
from tests.fakes import FakeFleet, run_llmdv
from tests.test_suites_against_fake import run_suites, write_profile

COMPLETIONS = get("completions")
CASE = "openai_completions.basic_non_streaming"


@pytest.mark.parametrize(
    ("text", "answer", "reasoning"),
    [
        ("ok", "ok", ""),
        ("<think>working it out</think>ok", "ok", "working it out"),
        ("<think>still thinking", "", "still thinking"),
    ],
)
def test_text_choices_preserve_output_and_usage_without_a_chat_role(text, answer, reasoning):
    body = {
        "object": "text_completion",
        "model": "m",
        "choices": [{"index": 0, "text": text, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }
    completion = COMPLETIONS.parse_completion(body)
    assert completion.role is None
    assert completion.has_output
    assert completion.text == answer
    assert completion.reasoning_text == reasoning
    assert completion.finish_reason == "length"
    assert completion.object_type == "text_completion"
    assert completion.model_echo == "m"
    assert completion.usage.prompt_tokens == 3
    assert completion.usage.completion_tokens == 4
    assert completion.usage.total_tokens == 7


def test_text_chunks_assemble_without_chat_deltas_and_keep_terminal_usage():
    chunks = [
        COMPLETIONS.parse_chunk(
            {
                "object": "text_completion",
                "model": "m",
                "choices": [{"text": text, "finish_reason": finish}],
            }
        )
        for text, finish in [("o", None), ("k", None), ("", "stop")]
    ]
    usage = COMPLETIONS.parse_chunk(
        {
            "choices": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )
    assert usage.is_terminal_usage
    completion = COMPLETIONS.assemble([*chunks, usage])
    assert completion.text == "ok"
    assert completion.role is None
    assert completion.finish_reason == "stop"
    assert completion.usage.total_tokens == 5


@pytest.mark.parametrize("aborted", [False, True])
def test_completions_probe_cli_runs_completions_and_rejects_http_200_abort(tmp_path, aborted):
    with FakeFleet(abort_finish_reason=aborted) as fleet:
        profile = write_profile(tmp_path, fleet)
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify",
            "--profile",
            str(profile),
            "--client",
            "completions-probe",
            "--local-output",
            str(run_dir),
            "--pod-logs",
            "off",
            "--evidence",
            "all",
        )

    assert result.returncode == (1 if aborted else 0), result.stdout + result.stderr
    records = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    assert {r["surface"] for r in records if r["surface"]} == {"completions"}
    (record,) = [r for r in records if r["case_id"] == CASE]
    assert record["outcome"] == ("fail" if aborted else "pass")
    assert record["http_status"] == 200
    checks = {c["name"]: c for c in record["checks"]}
    assert checks["finish_reason"]["ok"] is not aborted
    assert checks["finish_reason"]["observed"] == ("abort" if aborted else "stop")
    if aborted:
        assert "abandoned" in record["reason"]
    else:
        assert all(checks[name]["ok"] for name in ("object", "model_echo", "output_chars", "usage"))

    exchanges = [
        json.loads(line) for line in (run_dir / "exchanges.jsonl").read_text().splitlines()
    ]
    (sent,) = [e for e in exchanges if e["case_id"] == CASE]
    assert sent["url"].endswith("/v1/completions")
    assert isinstance(sent["request_body"]["prompt"], str)
    assert "messages" not in sent["request_body"]
    assert not sent["request_body"].get("stream")


@pytest.mark.parametrize(
    ("overrides", "failed_check"),
    [
        ({"object": "chat.completion"}, "object"),
        ({"model": "another/model"}, "model_echo"),
        ({"choices": []}, "finish_reason_recognised"),
        ({"choices": [{"text": "ok", "finish_reason": None}]}, "finish_reason_recognised"),
        ({"choices": [{"text": " ", "finish_reason": "stop"}]}, "output_chars"),
        ({"usage": None}, "usage"),
        ({"usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 99}}, "usage"),
    ],
)
def test_malformed_successful_completion_fails_with_evidence(tmp_path, overrides, failed_check):
    with FakeFleet(completion_overrides=overrides) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_completions_basic")

    assert code == 1
    (record,) = records
    assert record["case_id"] == CASE
    assert record["outcome"] == "fail"
    assert record["http_status"] == 200
    assert any(c["name"] == failed_check and not c["ok"] for c in record["checks"])


def test_completions_smoke_skips_when_the_ingress_does_not_declare_it(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet, surfaces=("chat_completions",))
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_completions_basic")
        assert not fleet.seen

    assert code == 0
    (record,) = records
    assert (record["outcome"], record["skip_class"]) == ("skip", "undeclared")
