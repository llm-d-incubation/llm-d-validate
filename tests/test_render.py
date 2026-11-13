import pytest
import yaml

from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize(
    "surface", ["chat_render", "completions_render", "messages_render", "responses_render"]
)
@pytest.mark.parametrize("defect", [None, "constant", "unstable", "boolean", "model"])
def test_render_contract(tmp_path, surface, defect):
    with FakeFleet(render=True, render_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = [surface]
        raw["clients"] = []
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path, tmp_path / "run", "-k", f"test_render_stability and {surface}"
        )
    assert code == (0 if defect is None else 1), records
    assert records[0]["outcome"] == ("pass" if defect is None else "fail"), records


def test_completions_render_requires_single_item_batch(tmp_path):
    with FakeFleet(render=True, render_defect="batch_shape") as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["completions_render"]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path, tmp_path / "run", "-k", "test_render_stability and completions_render"
        )
    assert code == 1 and records[0]["outcome"] == "fail", records


def test_render_unknown_models_fail_and_generation_cases_stay_separate(tmp_path):
    with FakeFleet(render=True) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["chat_render"]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path,
            tmp_path / "run",
            "-k",
            "test_render_unknown_model or test_unknown_model_rejected_without_fallback",
        )
    assert code == 0, records
    assert any(r["surface"] == "chat_render" and r["outcome"] == "pass" for r in records)
    assert not any(
        r["surface"] == "chat_render" and r["case_id"].startswith("discovery") for r in records
    )
