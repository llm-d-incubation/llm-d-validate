import json

import pytest
import yaml

from llmdv.http import sse
from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize("surface", ["chat_completions", "responses", "messages"])
@pytest.mark.parametrize("defect", [None, "auth_health", "health", "generation"])
def test_close_and_recovery(tmp_path, surface, defect):
    with FakeFleet(
        health_status=503 if defect == "health" else 401 if defect == "auth_health" else 200,
        abort_finish_reason=defect == "generation",
        response_overrides={"status": "failed"} if defect == "generation" else {},
    ) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = [surface]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_disconnect_recovery")
    assert code == (0 if defect in (None, "auth_health") else 1), records
    row = next(r for r in records if r["surface"] == surface)
    assert row["outcome"] == ("pass" if defect in (None, "auth_health") else "fail"), row
    exchanges = [
        json.loads(line) for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    assert exchanges[0]["url"].endswith("/health")
    if defect != "health":
        assert exchanges[1]["client_closed"] is True
        assert exchanges[2]["url"].endswith("/health")


def test_terminal_is_not_an_intentional_close(tmp_path):
    with FakeFleet(empty_chat_stream=True) as fleet:
        path = write_profile(tmp_path, fleet)
        code, records = run_suites(path, tmp_path / "run", "-k", "test_disconnect_recovery")
    assert code == 1, records
    assert any(r["outcome"] == "fail" and "ended before" in r["reason"] for r in records)


def test_event_budget_cannot_silently_pass():
    with pytest.raises(sse.StreamError, match="exceeded"):
        sse.read(["data: {}", "", "data: {}", ""], stop_when=lambda event: False, max_events=1)
    terminal = sse.read(["data: [DONE]", ""], stop_when=lambda event: True)
    assert terminal.saw_done and not terminal.client_closed


def test_shared_fleet_excludes_disconnect(tmp_path):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["fleet_policy"] = "shared"
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_disconnect_recovery")
        assert not fleet.seen
    assert code == 0
    assert all(r["outcome"] == "skip" for r in records)
