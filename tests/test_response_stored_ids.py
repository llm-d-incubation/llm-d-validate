import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from tests.fakes import FakeFleet, reasoning_only_response
from tests.test_suites_against_fake import run_suites, write_profile


def run(tmp_path, selector, capabilities=None, **fleet_flags):
    with FakeFleet(**fleet_flags) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        if capabilities is not None:
            raw = yaml.safe_load(path.read_text())
            raw["capabilities"] = capabilities
            path.write_text(yaml.safe_dump(raw))
        return run_suites(path, tmp_path / "run", "-k", selector)


@pytest.mark.parametrize(
    "selector,defect,outcome",
    [
        ("test_unknown_previous_response_id_is_refused", None, "pass"),
        ("test_unknown_previous_response_id_is_refused", "ignores_previous_id", "fail"),
        ("test_unstored_previous_response_id_is_refused", None, "pass"),
        ("test_unstored_previous_response_id_is_refused", "ignores_previous_id", "fail"),
        ("test_unknown_response_retrieval_is_refused", None, "pass"),
        ("test_unknown_response_retrieval_is_refused", "retrieve_server_error", "fail"),
    ],
)
def test_ids_nobody_stored_are_refused(tmp_path, selector, defect, outcome):
    code, records = run(tmp_path, selector, response_state_defect=defect)
    assert records and {r["outcome"] for r in records} == {outcome}, records
    assert code == (1 if outcome == "fail" else 0)


@pytest.mark.parametrize(
    "operation,flags,outcome",
    [
        ("retrieval", {}, "pass"),
        ("chaining", {}, "pass"),
        ("background", {}, "pass"),
        ("retrieval", {"stateful_responses": True}, "fail"),
        ("chaining", {"response_state_defect": "ignores_previous_id"}, "fail"),
        ("background", {"response_state_defect": "accepts_background"}, "fail"),
    ],
)
def test_stateless_contract(tmp_path, operation, flags, outcome):
    code, records = run(
        tmp_path,
        f"test_stateless_fleet_refuses_stored_state and {operation}",
        {"stateless_responses": True},
        **flags,
    )
    (record,) = records
    assert record["outcome"] == outcome, record
    assert code == (1 if outcome == "fail" else 0)


def test_stateless_contract_is_opt_in(tmp_path):
    code, records = run(tmp_path, "test_stateless_fleet_refuses_stored_state")
    assert code == 0
    assert records and all("undeclared" in r["reason"] for r in records)


@pytest.mark.parametrize(
    "defect,reasoning_only,outcome",
    [(None, False, "pass"), ("chain_ignored", False, "fail"), (None, True, "fail")],
)
def test_previous_response_id_chain(tmp_path, defect, reasoning_only, outcome):
    code, records = run(
        tmp_path,
        "test_previous_response_id_carries_context",
        {"stateful_responses": True},
        stateful_responses=True,
        response_state_defect=defect,
        response_overrides=reasoning_only_response() if reasoning_only else None,
    )
    (record,) = records
    assert record["outcome"] == outcome, record
    if reasoning_only:
        assert any(
            check["name"] == "answer_budget" and not check["ok"] for check in record["checks"]
        )
    assert code == (1 if outcome == "fail" else 0)


@pytest.mark.parametrize("other", ["stateful_responses", "background_responses"])
def test_stateless_declaration_excludes_storage(tmp_path, other):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"capabilities": {"stateless_responses": True, other: True}}))
    with pytest.raises(ProfileError, match="stateless_responses"):
        load(path)
