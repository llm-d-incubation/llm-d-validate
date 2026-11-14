import json

import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from tests.fakes import FakeFleet, thinking_blocks
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize(
    "mode,defect,passes",
    [
        ("enabled", None, True),
        ("enabled", "signature", False),
        ("enabled", "text", False),
        ("enabled", "order", False),
        ("enabled", "ignored", False),
        ("adaptive", "ignored", True),
        ("disabled", "ignored", True),
        ("disabled", None, False),
    ],
)
def test_thinking_contract_and_wire_request(tmp_path, mode, defect, passes):
    with FakeFleet(message_overrides={"content": thinking_blocks(defect)}) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["messages_thinking"] = [mode]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path,
            tmp_path / "run",
            "-k",
            "test_thinking_mode and not test_thinking_mode_streaming",
        )
        # Manual thinking runs at the minimum budget and at a larger one.
        assert len(fleet.seen) == (2 if mode == "enabled" else 1)
    assert code == (0 if passes else 1), records
    bodies = [
        json.loads(line)["request_body"]
        for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    for body in bodies:
        assert body["thinking"]["type"] == mode
        assert "temperature" not in body
        assert body["messages"][0]["content"] == "What is 17 times 23? Reply with the number only."
    if mode == "enabled":
        shapes = sorted((b["thinking"]["budget_tokens"], b["max_tokens"]) for b in bodies)
        assert shapes == [(1024, 2048), (2048, 4000)]
    assert any(
        c["name"] == "messages_thinking" and c["ok"] is passes for r in records for c in r["checks"]
    )
    if passes:
        assert all(c["ok"] for r in records for c in r["checks"] if c["name"] == "answer_only"), (
            records
        )


@pytest.mark.parametrize(
    "mode,defect,leak",
    [
        ("enabled", None, "untagged"),
        ("enabled", None, "glued"),
        ("enabled", None, "swallowed"),
        ("adaptive", None, "unopened"),
        ("disabled", "ignored", "untagged"),
    ],
)
def test_thinking_answer_must_be_alone(tmp_path, mode, defect, leak):
    """The thinking contract holds, but the text block carries reasoning or no answer."""
    with FakeFleet(message_overrides={"content": thinking_blocks(defect, leak)}) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["messages_thinking"] = [mode]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path,
            tmp_path / "run",
            "-k",
            "test_thinking_mode and not test_thinking_mode_streaming",
        )
    assert code == 1, records
    ran = [r for r in records if r["outcome"] != "skip"]
    assert ran and all(r["outcome"] == "fail" for r in ran)
    for record in ran:
        checks = {c["name"]: c["ok"] for c in record["checks"]}
        assert checks["messages_thinking"] and not checks["answer_only"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("messages_thinking", ["typo"]),
        ("messages_efforts", "high"),
        ("messages_efforts", ["low", "low"]),
    ],
)
def test_bad_options_rejected_at_load(tmp_path, field, value):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"capabilities": {field: value}}))
    with pytest.raises(ProfileError, match=field):
        load(path)


def test_effort_uses_output_config_and_checks_abort(tmp_path):
    with FakeFleet(message_overrides={"stop_reason": "abort"}) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["messages_efforts"] = ["high"]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_messages_effort")
    assert code == 1, records
    exchange = json.loads((tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()[0])
    assert exchange["request_body"]["output_config"] == {"effort": "high"}


@pytest.mark.parametrize(
    ("case", "declarations"),
    [
        ("test_thinking_mode_streaming", {"messages_thinking": ["enabled"]}),
        ("test_messages_effort_streaming", {"messages_efforts": ["high"]}),
    ],
)
def test_declared_thinking_stream_variants_reconstruct_native_output(tmp_path, case, declarations):
    with FakeFleet(anthropic={}) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"].update(declarations)
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", case)

    assert code == 0, records
    ran = [record for record in records if record["outcome"] != "skip"]
    assert len(ran) == 1
    checks = {check["name"]: check for check in ran[0]["checks"]}
    assert checks["messages_lifecycle"]["ok"]
    assert checks["finish_reason"]["ok"]
    assert checks["output_chars"]["ok"]
    assert checks["messages_thinking"]["ok"]
    exchanges = [
        json.loads(line) for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    request = next(exchange for exchange in exchanges if exchange["request_body"].get("stream"))
    assert request["request_body"]["stream"] is True


@pytest.mark.parametrize(
    ("defect", "failed_check"),
    [("stream_missing_signature", "messages_thinking"), ("stream_differs", "stream_stop_reason")],
)
def test_declared_thinking_stream_defects_are_caught(tmp_path, defect, failed_check):
    with FakeFleet(anthropic={"defect": defect}) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["messages_thinking"] = ["enabled"]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_thinking_mode_streaming")

    assert code == 1
    (record,) = [record for record in records if record["outcome"] == "fail"]
    assert any(check["name"] == failed_check and not check["ok"] for check in record["checks"])


def test_invalid_effort_is_rejected_with_evidence(tmp_path):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["messages_efforts"] = ["high"]
        path.write_text(yaml.safe_dump(raw))
        run_dir = tmp_path / "run"
        code, records = run_suites(path, run_dir, "-k", "test_invalid_effort")

    assert code == 0, records
    (record,) = records
    assert record["case_id"] == "anthropic_messages.invalid_effort"
    assert record["outcome"] == "pass"
    exchange = json.loads((run_dir / "exchanges.jsonl").read_text().splitlines()[0])
    assert exchange["request_body"]["output_config"] == {"effort": "llmdv-not-an-effort"}
    checks = {check["name"]: check for check in record["checks"]}
    assert all(
        checks[name]["ok"]
        for name in ("invalid_effort_generation", "invalid_field_value_status", "error_envelope")
    )


def test_invalid_effort_acceptance_fails_on_generation(tmp_path):
    with FakeFleet(ignore_bad_effort=True) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["messages_efforts"] = ["high"]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_invalid_effort")

    assert code == 1, records
    (record,) = records
    assert record["outcome"] == "fail"
    assert record["http_status"] == 200
    check = next(c for c in record["checks"] if c["name"] == "invalid_effort_generation")
    assert check["ok"] is False


def test_invalid_effort_skips_without_declared_efforts_or_post(tmp_path):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
        code, records = run_suites(path, tmp_path / "run", "-k", "test_invalid_effort")
        assert not any(method == "POST" for method, _, _ in fleet.seen)

    assert code == 0, records
    (record,) = records
    assert record["outcome"] == "skip"
    assert record["skip_class"] == "undeclared"


def test_invalid_effort_honours_declared_status(tmp_path):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["messages_efforts"] = ["high"]
        raw["ingresses"][0]["error_status"]["invalid_field_value"] = [422]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_invalid_effort")

    assert code == 1, records
    (record,) = records
    check = next(c for c in record["checks"] if c["name"] == "invalid_field_value_status")
    assert check["ok"] is False
    assert check["observed"] == 400


def test_invalid_effort_honours_unstructured_error_declaration(tmp_path):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["messages_efforts"] = ["high"]
        raw["ingresses"][0]["unstructured_errors"] = ["invalid_field_value"]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_invalid_effort")

    assert code == 1, records
    (record,) = records
    check = next(c for c in record["checks"] if c["name"] == "error_envelope")
    assert check["ok"] is False


def test_invalid_effort_acceptance_fails_even_when_status_200_is_declared(tmp_path):
    with FakeFleet(ignore_bad_effort=True) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["messages_efforts"] = ["high"]
        raw["ingresses"][0]["error_status"]["invalid_field_value"] = [200]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_invalid_effort")

    assert code == 1, records
    (record,) = records
    assert record["http_status"] == 200
    checks = {check["name"]: check for check in record["checks"]}
    assert checks["invalid_effort_generation"]["ok"] is False
    assert "invalid_field_value_status" not in checks


def test_malformed_http_200_cannot_masquerade_as_invalid_effort_rejection(tmp_path):
    with FakeFleet(ignore_bad_effort=True, message_overrides={"type": None}) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["messages_efforts"] = ["high"]
        raw["ingresses"][0]["error_status"]["invalid_field_value"] = [200]
        raw["ingresses"][0]["unstructured_errors"] = ["invalid_field_value"]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_invalid_effort")

    assert code == 1, records
    (record,) = records
    assert record["outcome"] == "fail"
    assert record["http_status"] == 200
    checks = {check["name"]: check for check in record["checks"]}
    assert checks["invalid_effort_generation"]["ok"] is True
    assert checks["invalid_effort_rejection_status"]["ok"] is False
    assert "invalid_field_value_status" not in checks
    assert "error_envelope" not in checks


def test_declared_server_error_cannot_masquerade_as_invalid_effort_rejection(tmp_path):
    with FakeFleet(bad_effort_status=500) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["messages_efforts"] = ["high"]
        raw["ingresses"][0]["error_status"]["invalid_field_value"] = [500]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_invalid_effort")

    assert code == 1, records
    (record,) = records
    assert record["outcome"] == "fail"
    assert record["http_status"] == 500
    checks = {check["name"]: check for check in record["checks"]}
    assert checks["invalid_effort_generation"]["ok"] is True
    assert checks["invalid_effort_rejection_status"]["ok"] is False
    assert "invalid_field_value_status" not in checks
    assert "error_envelope" not in checks


def test_invalid_effort_reports_each_ingress_independently(tmp_path):
    with FakeFleet() as faithful, FakeFleet(ignore_bad_effort=True) as dropping:
        path = write_profile(tmp_path, faithful)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["messages_efforts"] = ["high"]
        raw["ingresses"].append(
            {**raw["ingresses"][0], "id": "dropping", "base_url": dropping.base_url}
        )
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_invalid_effort")

    assert code == 1, records
    assert {(record["case_id"], record["ingress"]) for record in records} == {
        ("anthropic_messages.invalid_effort", "fake"),
        ("anthropic_messages.invalid_effort", "dropping"),
    }
    assert {record["ingress"]: record["outcome"] for record in records} == {
        "fake": "pass",
        "dropping": "fail",
    }


def test_invalid_field_value_error_status_loads(tmp_path):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["error_status"]["invalid_field_value"] = [400]
        path.write_text(yaml.safe_dump(raw))
        profile = load(path)

    assert profile.ingress("fake").expected_error_status("invalid_field_value") == (400,)
