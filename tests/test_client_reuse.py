import json

import pytest
import yaml

from tests.fakes import MODEL, FakeFleet, fake_claude_binary
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize(
    "defect,outcome",
    [
        (None, "pass"),
        ("stream", "pass"),
        ("collapse", "fail"),
        ("lost_history", "fail"),
        ("replica", "skip"),
        ("retry", "fail"),
    ],
)
def test_three_turn_client_reuse(tmp_path, defect, outcome):
    binary = fake_claude_binary(tmp_path / "claude", defect=defect)
    with FakeFleet(cache_probe=True, cache_defect=defect) as fleet:
        code, records = _run_reuse(tmp_path, fleet, binary)
        if defect == "retry":
            assert len(fleet.seen) == 1
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records

    if defect == "collapse":
        assert "cache reuse collapsed" in records[0]["reason"]
    elif defect == "lost_history":
        assert "lost the initial reference" in records[0]["reason"]
    elif defect == "retry":
        assert "retried" in records[0]["reason"]


@pytest.mark.parametrize("status", [404, 500])
def test_a_failed_first_turn_is_blamed_on_its_status_not_the_refused_retry(tmp_path, status):
    binary = fake_claude_binary(tmp_path / "claude", defect="fallback")
    with FakeFleet(cache_probe=True, model_errors={MODEL: status}) as fleet:
        code, records = _run_reuse(tmp_path, fleet, binary)
        assert len(fleet.seen) == 1
    record = records[0]
    assert code == 1 and record["outcome"] == "fail", records
    assert record["reason"].startswith("AssertionError: client_upstream_statuses"), record
    assert f"{status}: 1" in record["reason"] and "refused" in record["reason"], record
    turn = next(c for c in record["checks"] if c["name"] == "client_turn_requests")
    assert not turn["ok"] and f"upstream {status}" in str(turn["observed"]), turn
    assert not any(c["name"] == "client_process" for c in record["checks"]), record


def test_a_client_that_streams_first_reuses_on_a_healthy_fleet(tmp_path):
    binary = fake_claude_binary(tmp_path / "claude", defect="fallback")
    with FakeFleet(cache_probe=True) as fleet:
        code, records = _run_reuse(tmp_path, fleet, binary)
    assert code == 0, records
    turns = [c for c in records[0]["checks"] if c["name"] == "client_turn_requests"]
    assert len(turns) == 3 and all(c["ok"] for c in turns), turns


def _run_reuse(tmp_path, fleet, binary, *extra):
    path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["clients"] = [
        {
            "id": "cli",
            "surfaces": ["messages"],
            "binaries": [{"version": "2.1.0", "executable": str(binary)}],
        }
    ]
    raw["model"]["max_model_len"] = 4096
    raw["capabilities"] = {
        "client_reuse": True,
        "cache_reuse": {
            "min_ratio": 0.8,
            "prefix_words": 128,
            "surfaces": ["messages"],
            "replica_header": "x-llmdv-replica",
        },
    }
    path.write_text(yaml.safe_dump(raw))
    return run_suites(path, tmp_path / "run", "-k", "test_claude_code_multiturn_reuse", *extra)


def test_reuse_leaves_room_for_a_model_that_always_thinks(tmp_path):
    """A model that cannot turn thinking off still answers each turn of the reuse session.

    GLM-5.3 thinks whatever the request asks. Held to the probe's 256-token output budget, the
    session spent it all thinking, ended without text and failed on a healthy fleet.
    """
    binary = fake_claude_binary(tmp_path / "claude")
    with FakeFleet(cache_probe=True, forced_thinking_tokens=300) as fleet:
        code, records = _run_reuse(tmp_path, fleet, binary, "--llmdv-evidence", "all")
    assert records[0]["outcome"] == "pass", records
    assert code == 0
    exchanges = [
        json.loads(line) for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    replies = [e["response_body"] for e in exchanges if e["url"].endswith("/v1/messages")]
    assert len(replies) == 3
    assert all(reply["content"][0]["type"] == "thinking" for reply in replies)
