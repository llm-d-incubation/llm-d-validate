import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from tests.fakes import FakeFleet
from tests.test_cache_reuse import BLOCKS, run_cache_case
from tests.test_suites_against_fake import write_profile

RATIO = {"min_ratio": 0.8, "cold_max_ratio": 0.1, "single_replica": True}


@pytest.mark.parametrize("surface", ["chat_completions", "messages", "responses"])
@pytest.mark.parametrize("declaration", [RATIO, BLOCKS], ids=["ratio", "blocks"])
@pytest.mark.parametrize(
    "defect",
    # A cold fleet fails the warm control; the others each leak or lose the salted prefix.
    [None, "ignored_salt", "cold", "unsalted_leak", "salt_flush"],
)
def test_salt_isolation_has_a_warm_control(tmp_path, surface, declaration, defect):
    code, records = run_cache_case(
        tmp_path,
        surface,
        declaration,
        "test_cache_salt_isolation",
        defect,
        vllm_options=["cache_salt"],
    )
    assert code == (0 if defect is None else 1), records
    assert records[0]["outcome"] == ("pass" if defect is None else "fail"), records
    if defect == "unsalted_leak":
        assert "no salt" in records[0]["reason"], records
    if defect == "salt_flush":
        assert "below" in records[0]["reason"], records


def test_salt_isolation_needs_a_cold_rule(tmp_path):
    declaration = {"min_ratio": 0.8, "single_replica": True}
    code, records = run_cache_case(
        tmp_path,
        "chat_completions",
        declaration,
        "test_cache_salt_isolation",
        vllm_options=["cache_salt"],
    )
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "[undeclared]" in records[0]["reason"], records


@pytest.mark.parametrize("value", [True, -1, 0.8, 1, "0.1", float("nan")])
def test_cold_threshold_cannot_overlap_warm_contract(tmp_path, value):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"] = {
        "cache_reuse": {"min_ratio": 0.8, "cold_max_ratio": value, "single_replica": True}
    }
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ProfileError, match="cold_max_ratio"):
        load(path)
