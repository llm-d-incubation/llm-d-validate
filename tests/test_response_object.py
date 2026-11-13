import pytest

from tests.fakes import FakeFleet, FakeResponsesConfig
from tests.test_suites_against_fake import run_suites, write_profile


def run(tmp_path, selector, defect):
    with FakeFleet(response_config=FakeResponsesConfig(shape_defect=defect)) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        code, records = run_suites(path, tmp_path / "run", "-k", selector)
    return code, {r["nodeid"].rstrip("]").rsplit("-", 1)[1]: r for r in records}


@pytest.mark.parametrize(
    "defect,check",
    [
        (None, None),
        ("missing_fields", "response_object_fields"),
        # The stream lifecycle already refuses a second item under the same id.
        ("duplicate_ids", "response_object_shape|responses_item_order"),
        ("no_annotations", "response_object_shape"),
        ("no_usage_details", "response_object_shape"),
    ],
)
def test_response_object(tmp_path, defect, check):
    code, records = run(tmp_path, "test_response_object_fields", defect)
    assert set(records) == {"json", "sse"}
    for record in records.values():
        failed = {c["name"] for c in record["checks"] if not c["ok"]}
        if check is None:
            assert record["outcome"] == "pass", record
        else:
            assert record["outcome"] == "fail" and set(check.split("|")) & failed, record
    assert code == (1 if check else 0)


@pytest.mark.parametrize(
    "defect,check",
    [
        (None, None),
        ("limit_completed", "incomplete_status"),
        ("limit_no_reason", "incomplete_status"),
        ("limit_over_cap", "output_tokens_within_cap"),
    ],
)
def test_token_cap(tmp_path, defect, check):
    code, records = run(tmp_path, "test_token_cap_reports_incomplete", defect)
    assert set(records) == {"json", "sse"}
    for record in records.values():
        failed = {c["name"] for c in record["checks"] if not c["ok"]}
        if check is None:
            assert record["outcome"] == "pass", record
        else:
            assert record["outcome"] == "fail" and set(check.split("|")) & failed, record
    assert code == (1 if check else 0)
