"""An HTTP 200 with missing, invalid or impossible reasoning accounting must fail."""

import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from tests.fakes import FakeFleet, reasoning_usage_payload
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize("surface", ["chat_completions", "responses"])
@pytest.mark.parametrize(
    "count,passes", [(1, True), (None, False), (-1, False), (True, False), ("1", False), (3, False)]
)
def test_native_accounting(tmp_path, surface, count, passes):
    key = "chat_overrides" if surface == "chat_completions" else "response_overrides"
    with FakeFleet(**{key: reasoning_usage_payload(surface, count)}) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = [surface]
        raw["capabilities"]["reasoning_tokens"] = [surface]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_reasoning_usage")
    assert code == (0 if passes else 1), records
    row = next(r for r in records if r["surface"] == surface)
    check = next(c for c in row["checks"] if c["name"] == "reasoning_usage")
    assert check["ok"] is passes
    assert check["observed"]["reasoning_tokens"] == count


def test_visible_reasoning_with_zero_tokens_fails(tmp_path):
    with FakeFleet(chat_overrides=reasoning_usage_payload("chat_completions", 0)) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["reasoning_tokens"] = ["chat_completions"]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_reasoning_usage")
    assert code == 1, records
    assert any("reasoning usage" in (r["reason"] or "") for r in records)


def test_undeclared_accounting_sends_no_requests(tmp_path):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
        code, records = run_suites(path, tmp_path / "run", "-k", "test_reasoning_usage")
        assert not fleet.seen
    assert code == 0
    assert records and all(r["outcome"] == "skip" for r in records)


@pytest.mark.parametrize("value", ["true", 1, ["typo"], [False]])
def test_bad_capability_is_a_profile_error(tmp_path, value):
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump({"capabilities": {"reasoning_tokens": value}}))
    with pytest.raises(ProfileError, match="capabilities.reasoning_tokens"):
        load(path)
