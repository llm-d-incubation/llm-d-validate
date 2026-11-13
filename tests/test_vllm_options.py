import pytest
import yaml

from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize(
    "surface,option",
    [
        ("chat_completions", name)
        for name in ("return_token_ids", "min_tokens", "ignore_eos", "truncate_prompt_tokens")
    ]
    + [("responses", "ignore_eos")],
)
@pytest.mark.parametrize("defect", [None, "ignored"])
def test_option_effects(tmp_path, surface, option, defect):
    with FakeFleet(vllm_options=True, vllm_option_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = [surface]
        raw["capabilities"] = {"vllm_options": [option]}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path, tmp_path / "run", "-k", f"test_vllm_observable_options and {option} and {surface}"
        )
    assert code == (0 if defect is None else 1), records
    assert records[0]["outcome"] == ("pass" if defect is None else "fail"), records


def test_boolean_token_ids_fail(tmp_path):
    with FakeFleet(vllm_options=True, vllm_option_defect="bool_ids") as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"] = {"vllm_options": ["return_token_ids"]}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path,
            tmp_path / "run",
            "-k",
            "test_vllm_observable_options and return_token_ids and chat_completions",
        )
    assert code == 1, records
    assert records[0]["outcome"] == "fail"
