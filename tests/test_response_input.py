import pytest
import yaml

from tests.fakes import FakeFleet, FakeResponsesConfig, reasoning_only_response
from tests.test_suites_against_fake import run_suites, write_profile


def run(tmp_path, selector, defect):
    with FakeFleet(response_config=FakeResponsesConfig(input_defect=defect)) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        return run_suites(path, tmp_path / "run", "-k", selector)


@pytest.mark.parametrize(
    "defect,outcome",
    [(None, "pass"), ("drops_instructions", "fail"), ("changes_instructions", "fail")],
)
def test_instructions(tmp_path, defect, outcome):
    code, records = run(tmp_path, "test_instructions_reach_the_model", defect)
    (record,) = records
    assert record["outcome"] == outcome, record
    assert code == (1 if outcome == "fail" else 0)


@pytest.mark.parametrize(
    "shape,defect",
    [
        ("system", "drops_role:system"),
        ("developer", "drops_role:developer"),
        ("developer_with_instructions", "instructions_replace_system"),
        ("input_text_parts", "first_part_only"),
        ("assistant_replay", "drops_assistant"),
        ("reasoning_replay", "rejects_reasoning"),
        ("reasoning_summary_replay", "rejects_reasoning"),
        ("assistant_phase", "rejects_phase"),
        ("assistant_text", "drops_assistant"),
    ],
)
@pytest.mark.parametrize("defective", [False, True])
def test_input_items(tmp_path, shape, defect, defective):
    selector = f"test_input_items_reach_the_model and {shape}"
    if shape == "developer":
        selector += " and not instructions"
    code, records = run(tmp_path, selector, defect if defective else None)
    (record,) = records
    assert record["outcome"] == ("fail" if defective else "pass"), record
    assert code == (1 if defective else 0)


def test_input_checks_require_an_answer_when_reasoning_exhausts_the_budget(tmp_path):
    with FakeFleet(response_overrides=reasoning_only_response()) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        code, records = run_suites(
            path, tmp_path / "run", "-k", "test_instructions_reach_the_model"
        )
    (record,) = records
    assert record["outcome"] == "fail", record
    assert any(check["name"] == "answer_budget" and not check["ok"] for check in record["checks"])
    assert code == 1


@pytest.mark.parametrize(
    "defect,outcome",
    [(None, "pass"), ("drops_assistant", "fail"), ("rejects_null_fields", "fail")],
)
def test_output_replay(tmp_path, defect, outcome):
    code, records = run(tmp_path, "test_output_items_replay_as_returned", defect)
    (record,) = records
    assert record["outcome"] == outcome, record
    assert code == (1 if outcome == "fail" else 0)


@pytest.mark.parametrize(
    "declared,defect,outcome",
    [
        (False, None, "skip"),
        (True, None, "pass"),
        (True, "rejects_encrypted", "fail"),
        (True, "drops_assistant", "fail"),
    ],
)
def test_encrypted_replay(tmp_path, declared, defect, outcome):
    with FakeFleet(
        response_config=FakeResponsesConfig(
            input_defect=defect,
            encrypted_reasoning=True,
        )
    ) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        if declared:
            raw = yaml.safe_load(path.read_text())
            raw["capabilities"]["responses_features"] = {"encrypted_reasoning": "accepted"}
            path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_encrypted_reasoning_replays")
    (record,) = records
    assert record["outcome"] == outcome, record
    assert code == (1 if outcome == "fail" else 0)


@pytest.mark.parametrize(
    "form,defect,outcome",
    [
        ("text", None, "pass"),
        ("parts", None, "pass"),
        ("parts", "rejects_output_parts", "fail"),
        ("parts", "drops_output_parts", "fail"),
        ("text", "rejects_output_parts", "pass"),
        ("text", "drops_output_parts", "pass"),
    ],
)
def test_function_output(tmp_path, form, defect, outcome):
    with FakeFleet(response_config=FakeResponsesConfig(input_defect=defect)) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["model"]["tools"] = {"parser": "example"}
        path.write_text(yaml.safe_dump(raw))
        selector = f"test_function_output_reaches_the_model and {form}"
        code, records = run_suites(path, tmp_path / "run", "-k", selector)
    (record,) = records
    assert record["outcome"] == outcome, record
    assert code == (1 if outcome == "fail" else 0)


def test_function_output_needs_tools(tmp_path):
    code, records = run(tmp_path, "test_function_output_reaches_the_model", None)
    assert {r["outcome"] for r in records} == {"skip"}
    assert code == 0
