import pytest
import yaml

from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize(
    "surface,fixture",
    [
        ("chat_render", "synthetic_chat_billing"),
        ("messages_render", "synthetic_messages_reminder"),
        ("responses_render", "synthetic_responses_prefix"),
    ],
)
@pytest.mark.parametrize(
    "defect", [None, "long_template", "hoisted", "mid_prefix", "constant", "unstable"]
)
def test_prefix_probe_catches_hoisted_volatile_text(tmp_path, surface, fixture, defect):
    with FakeFleet(render=True, render_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = [surface]
        raw["capabilities"] = {"prefix_stability": [surface]}
        raw["clients"] = []
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path,
            tmp_path / "run",
            "-k",
            f"test_synthetic_prefix_stability and {fixture}",
        )
    healthy = defect in (None, "long_template")
    assert code == (0 if healthy else 1), records
    assert records[0]["outcome"] == ("pass" if healthy else "fail"), records
