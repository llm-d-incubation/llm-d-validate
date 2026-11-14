"""Exercise the shipped schema case through real HTTP on all three wire shapes."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
import yaml

from llmdv.assertions.structured_output import (
    assert_structured_output,
    assert_structured_reasoning,
)
from llmdv.evidence import journal
from llmdv.profile.schema import Model, Reasoning
from llmdv.surfaces.base import Completion, Usage
from tests.fakes import FakeFleet, reasoning_usage_payload, run_llmdv
from tests.test_suites_against_fake import run_suites, write_profile

CASE = "field_options.structured_output"
SURFACES = {"chat_completions", "messages", "responses"}
REASONING_MODEL = Model(
    "m", reasoning=Reasoning(parser="example", field_name="reasoning", default_on=True)
)


def exchanges(run_dir):
    return [json.loads(line) for line in (run_dir / "exchanges.jsonl").read_text().splitlines()]


def test_schema_is_honoured_through_each_surface_and_ingress_with_evidence(tmp_path):
    with FakeFleet() as first, FakeFleet() as second:
        profile = write_profile(tmp_path, first, responses=True, structured_output=True)
        config = yaml.safe_load(profile.read_text())
        other = deepcopy(config["ingresses"][0])
        other.update(id="second", base_url=second.base_url)
        config["ingresses"].append(other)
        profile.write_text(yaml.safe_dump(config))
        run_dir = tmp_path / "run"
        code, records = run_suites(
            profile, run_dir, "-k", "test_structured_output", "--llmdv-evidence", "all"
        )

    assert code == 0
    assert len(records) == 6
    assert {(r["surface"], r["ingress"]) for r in records} == {
        (surface, ingress) for surface in SURFACES for ingress in ("fake", "second")
    }
    markers = set()
    for record in records:
        assert record["case_id"] == CASE and record["outcome"] == "pass"
        checked = {c["name"] for c in record["checks"] if c["ok"]}
        assert {"object", "model_echo", "output_chars", "usage"} <= checked
        if record["surface"] != "responses":
            assert "role" in checked
        check = next(c for c in record["checks"] if c["name"] == "structured_output_schema")
        assert check["ok"] and check["observed"]["count"] == 1
        markers.add(check["observed"]["marker"])
    assert len(markers) == 6

    sent = exchanges(run_dir)
    assert len(sent) == 6
    for exchange in sent:
        body = exchange["request_body"]
        assert body["temperature"] == 0 and not body.get("stream")
        assert body["model"] == first.model
        if exchange["url"].endswith("/chat/completions"):
            assert body["response_format"]["type"] == "json_schema"
            output_format = body["response_format"]["json_schema"]
            assert output_format["strict"] is True and output_format["name"]
            prompt = body["messages"][0]["content"]
        elif exchange["url"].endswith("/responses"):
            output_format = body["text"]["format"]
            assert output_format["type"] == "json_schema"
            assert output_format["strict"] is True and output_format["name"]
            assert body["store"] is False
            prompt = body["input"]
        else:
            assert exchange["url"].endswith("/messages")
            output_format = body["output_config"]["format"]
            assert set(output_format) == {"type", "schema"}
            assert output_format["type"] == "json_schema"
            prompt = body["messages"][0]["content"]
        schema = output_format["schema"]
        (marker,) = schema["properties"]["marker"]["enum"]
        assert marker in markers and marker not in prompt
        assert body.get("max_tokens", body.get("max_output_tokens")) == 1024


@pytest.mark.parametrize(
    ("defect", "check_name"),
    [
        ("ignores_schema", "structured_output_schema"),
        ("wrong_type", "structured_output_schema"),
        ("missing_field", "structured_output_schema"),
        ("extra_field", "structured_output_schema"),
        ("invalid_json", "structured_output_json"),
        ("reasoning_only", "structured_output_json"),
    ],
)
def test_broken_structured_output_fails_with_evidence_on_every_surface(
    tmp_path, defect, check_name
):
    with FakeFleet(structured_output_defect=defect) as fleet:
        profile = write_profile(tmp_path, fleet, responses=True, structured_output=True)
        run_dir = tmp_path / "run"
        code, records = run_suites(profile, run_dir, "-k", "test_structured_output")

    assert code == 1
    assert {r["surface"] for r in records} == SURFACES
    for record in records:
        assert record["outcome"] == "fail" and record["http_status"] == 200
        failed = [c for c in record["checks"] if not c["ok"]]
        assert [c["name"] for c in failed] == [check_name]
    assert len(exchanges(run_dir)) == 3  # No retries or repair requests.


REASONING_SURFACES = ["chat_completions", "responses"]


@pytest.mark.parametrize("reasons", [True, False])
def test_a_reasoning_fleet_must_reason_before_a_schema_answer(tmp_path, reasons):
    """A grammar held from the first token leaves valid JSON and no reasoning: a failure where
    the profile declares reasoning on, judged after the schema so the answer is still checked."""
    overrides = (
        {
            "chat_overrides": reasoning_usage_payload("chat_completions", 4),
            "response_overrides": reasoning_usage_payload("responses", 4),
        }
        if reasons
        else {}
    )
    # Inline <think> text would count as reasoning; this fleet reasons only where told to.
    with FakeFleet(inline_thinking=False, **overrides) as fleet:
        profile = write_profile(tmp_path, fleet, responses=True)
        config = yaml.safe_load(profile.read_text())
        config["capabilities"]["structured_output"] = REASONING_SURFACES
        config["model"]["reasoning"] = {
            "parser": "example",
            "field": "reasoning_content",
            "default_on": True,
        }
        profile.write_text(yaml.safe_dump(config))
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_structured_output")

    assert code == (0 if reasons else 1)
    judged = [r for r in records if r["surface"] in REASONING_SURFACES]
    assert len(judged) == 2
    for record in judged:
        assert record["outcome"] == ("pass" if reasons else "fail")
        check = next(c for c in record["checks"] if c["name"] == "structured_output_reasoning")
        assert check["ok"] is reasons
        schema = next(c for c in record["checks"] if c["name"] == "structured_output_schema")
        assert schema["ok"]


def test_reasoning_is_not_required_where_the_profile_declares_no_parser(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet, responses=True, structured_output=True)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_structured_output")

    assert code == 0
    assert not [
        c for r in records for c in r["checks"] if c["name"] == "structured_output_reasoning"
    ]


@pytest.mark.parametrize(
    "reasoning,count,ok",
    [("", None, False), ("", 0, False), ("weighing it up", None, True), ("", 12, True)],
)
def test_reasoning_text_or_a_positive_count_shows_the_model_reasoned(reasoning, count, ok):
    completion = Completion(
        role="assistant",
        text='{"a": 1}',
        finish_reason="stop",
        usage=Usage(prompt_tokens=3, completion_tokens=14, reasoning_tokens=count),
        reasoning_text=reasoning,
    )
    active = journal.activate("structured-reasoning")
    try:
        if ok:
            assert_structured_reasoning(completion, REASONING_MODEL)
        else:
            with pytest.raises(AssertionError, match="structured_output_reasoning"):
                assert_structured_reasoning(completion, REASONING_MODEL)
        assert [(c.name, c.ok) for c in active.checks] == [("structured_output_reasoning", ok)]
        assert_structured_reasoning(completion, Model("m", reasoning=Reasoning(default_on=True)))
        assert len(active.checks) == 1
    finally:
        journal.deactivate()


@pytest.mark.parametrize("missing", ["capability", "false", "surfaces"])
def test_undeclared_structured_output_skips_without_requests(tmp_path, missing):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet, responses=True, structured_output=True)
        config = yaml.safe_load(profile.read_text())
        if missing == "capability":
            del config["capabilities"]["structured_output"]
        elif missing == "false":
            config["capabilities"]["structured_output"] = False
        else:
            config["ingresses"][0]["surfaces"] = ["completions"]
            config["clients"] = []
        profile.write_text(yaml.safe_dump(config))
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_structured_output")
        assert not fleet.seen

    assert code == 0 and len(records) == 3
    assert {(r["outcome"], r["skip_class"]) for r in records} == {("skip", "undeclared")}


@pytest.mark.parametrize("selected", [[], ["chat_completions"], ["chat_completions", "responses"]])
def test_surface_list_runs_only_declared_structured_output_paths(tmp_path, selected):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet, responses=True)
        config = yaml.safe_load(profile.read_text())
        config["capabilities"]["structured_output"] = selected
        profile.write_text(yaml.safe_dump(config))
        run_dir = tmp_path / "run"
        code, records = run_suites(profile, run_dir, "-k", "test_structured_output")
        assert len(fleet.seen) == len(selected)

    assert code == 0 and len(records) == 3
    for record in records:
        if record["surface"] in selected:
            assert record["outcome"] == "pass"
        else:
            assert (record["outcome"], record["skip_class"]) == ("skip", "undeclared")
            assert record["surface"] in record["reason"]
    if selected:
        assert {e["surface"] for e in exchanges(run_dir)} == set(selected)


@pytest.mark.parametrize(
    ("defect", "check_name", "selected"),
    [
        ("wrong_model_echo", "model_echo", sorted(SURFACES)),
        ("wrong_object", "object", sorted(SURFACES)),
        ("wrong_role", "role", ["chat_completions", "messages"]),
    ],
)
def test_valid_json_cannot_hide_a_broken_completion_contract(
    tmp_path, defect, check_name, selected
):
    with FakeFleet(structured_output_defect=defect) as fleet:
        profile = write_profile(tmp_path, fleet, responses=True)
        config = yaml.safe_load(profile.read_text())
        config["capabilities"]["structured_output"] = selected
        profile.write_text(yaml.safe_dump(config))
        run_dir = tmp_path / "run"
        code, records = run_suites(profile, run_dir, "-k", "test_structured_output")

    assert code == 1
    for record in records:
        if record["surface"] not in selected:
            assert record["skip_class"] == "undeclared"
            continue
        assert record["outcome"] == "fail" and record["http_status"] == 200
        assert any(c["name"] == check_name and not c["ok"] for c in record["checks"])
    assert len(exchanges(run_dir)) == len(selected)


def test_abort_cannot_pass_even_with_a_valid_structured_answer(tmp_path):
    with FakeFleet(abort_finish_reason=True) as fleet:
        profile = write_profile(tmp_path, fleet, responses=True, structured_output=True)
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_structured_output")

    assert code == 1 and len(records) == 3
    assert all(r["outcome"] == "fail" and r["http_status"] == 200 for r in records)
    assert all(
        any(not c["ok"] and c["name"].startswith("finish_reason") for c in r["checks"])
        for r in records
    )


@pytest.mark.parametrize("declared", [True, False])
def test_throttling_keeps_its_existing_classification(tmp_path, declared):
    with FakeFleet(throttle=True) as fleet:
        profile = write_profile(
            tmp_path, fleet, responses=True, structured_output=True, rate_limited=declared
        )
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_structured_output")

    assert code == (0 if declared else 1) and len(records) == 3
    for record in records:
        assert record["outcome"] == ("skip" if declared else "fail")
        if declared:
            assert record["skip_class"] == "throttled"


def test_cli_client_filter_selects_only_its_structured_surfaces(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet, responses=True, structured_output=True)
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify",
            "--profile",
            str(profile),
            "--client",
            "agent",
            "-k",
            "test_structured_output",
            "--local-output",
            str(run_dir),
            "--pod-logs",
            "off",
        )

    assert result.returncode == 0, result.stdout + result.stderr
    records = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    assert {r["surface"] for r in records} == {"chat_completions", "messages"}
    assert all(r["outcome"] == "pass" for r in records)


@pytest.mark.parametrize("text", ["", "```json\n{}\n```", "{} trailing", "{", "NaN", "Infinity"])
def test_answer_must_be_json_without_repair(text):
    active = journal.activate("invalid-json")
    try:
        with pytest.raises(AssertionError, match="not valid JSON"):
            assert_structured_output(text, {})
        assert len(active.checks) == 1
        assert not active.checks[0].ok and active.checks[0].observed == text
    finally:
        journal.deactivate()
