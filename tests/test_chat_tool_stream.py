import pytest
import yaml

from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "prose_result",
        "dropped_fragment",
        "id_changed",
        "index",
        "name",
        "argument_type",
        "schema",
        "abort",
        "ignores_result",
    ],
)
def test_streamed_tool_round_trip(tmp_path, defect):
    with FakeFleet(chat_tools=True, chat_tool_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["chat_completions"]
        raw["model"]["tools"] = {"parser": "example"}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_streamed_tool_loop")
    assert code == (0 if defect in (None, "prose_result") else 1), records
    assert records[0]["outcome"] == ("pass" if defect in (None, "prose_result") else "fail")
    if defect not in (None, "prose_result"):
        assert any(c.get("ok") is False for c in records[0]["checks"])
