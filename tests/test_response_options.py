import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize(
    "option,defect,passes",
    [
        ("metadata", None, True),
        ("metadata", "ignored", False),
        ("logprobs", None, True),
        ("logprobs", "ignored", False),
        ("logprobs", "invalid_logprob", False),
        ("reasoning", None, True),
        ("reasoning", "ignored", False),
    ],
)
def test_option_contract(tmp_path, option, defect, passes):
    with FakeFleet(response_options=True, response_option_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"].append("responses")
        key, values = (
            ("responses_reasoning_efforts", ["high"])
            if option == "reasoning"
            else ("responses_options", [option])
        )
        raw["capabilities"][key] = values
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path,
            tmp_path / "run",
            "-k",
            "test_response_field_option or test_response_reasoning_effort",
        )
        assert len(fleet.seen) == 1
    assert code == (0 if passes else 1), records
    assert any(r["outcome"] == ("pass" if passes else "fail") for r in records)


@pytest.mark.parametrize(
    "field,value", [("responses_options", ["unknown"]), ("responses_reasoning_efforts", "high")]
)
def test_invalid_option_declarations(tmp_path, field, value):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"capabilities": {field: value}}))
    with pytest.raises(ProfileError, match=field):
        load(path)


@pytest.mark.parametrize(
    "defect,outcome",
    [(None, "pass"), ("stream_logprobs_dropped", "fail"), ("invalid_logprob", "fail")],
)
def test_streamed_logprobs(tmp_path, defect, outcome):
    with FakeFleet(response_options=True, response_option_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["responses_options"] = ["logprobs"]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_streamed_logprobs")
    (record,) = records
    assert record["outcome"] == outcome, record
    assert code == (1 if outcome == "fail" else 0)
