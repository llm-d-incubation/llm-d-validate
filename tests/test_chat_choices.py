import httpx
import pytest
import yaml

from llmdv.surfaces.chat_completions import CHAT_COMPLETIONS
from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_fake_rejects_greedy_multiple_choices(stream):
    with FakeFleet(chat_options=True) as fleet:
        body = CHAT_COMPLETIONS.minimal_body(fleet.model, n=2, stream=stream)
        response = httpx.post(f"{fleet.base_url}/v1/chat/completions", json=body, trust_env=False)

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "n must be 1 when using greedy sampling, got 2."


@pytest.mark.parametrize(
    "defect,stream,passes",
    [
        (None, False, True),
        (None, True, True),
        ("ignored_n", False, False),
        ("ignored_n", True, False),
        ("duplicate_index", False, False),
        ("duplicate_index", True, False),
        ("abort", False, False),
        ("abort", True, False),
        ("missing_finish", True, False),
        ("late_delta", True, False),
        ("missing_usage", True, False),
    ],
)
def test_all_choices_are_checked(tmp_path, defect, stream, passes):
    with FakeFleet(chat_options=True, chat_option_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["chat_options"] = ["n"]
        path.write_text(yaml.safe_dump(raw))
        select = "test_multiple_choices and " + ("sse" if stream else "json")
        code, records = run_suites(path, tmp_path / "run", "-k", select)
        assert len(fleet.seen) == 1
    assert code == (0 if passes else 1), records
    assert records[0]["outcome"] == ("pass" if passes else "fail")
    if passes:
        assert sum(check["name"] == "usage" for check in records[0]["checks"]) == 1
        answers = [c for c in records[0]["checks"] if c["name"] == "answer_only"]
        assert len(answers) == 2 and all(c["ok"] for c in answers)
        objects = [c["observed"] for c in records[0]["checks"] if c["name"] == "object"]
        assert objects == ["chat.completion.chunk" if stream else "chat.completion"] * 2
    assert any(c["ok"] is passes for c in records[0]["checks"] if "ok" in c)


@pytest.mark.parametrize("defect,passes", [(None, True), ("ignored_logprobs", False)])
def test_logprobs_cannot_be_dropped(tmp_path, defect, passes):
    with FakeFleet(chat_options=True, chat_option_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["chat_options"] = ["logprobs"]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_chat_output_logprobs")
    assert code == (0 if passes else 1), records


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_every_choice_must_answer_alone(tmp_path, stream):
    """The second choice carries its reasoning in content; the first is fine."""
    with FakeFleet(chat_options=True, chat_option_defect="leaked_reasoning") as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["chat_options"] = ["n"]
        path.write_text(yaml.safe_dump(raw))
        select = "test_multiple_choices and " + ("sse" if stream else "json")
        code, records = run_suites(path, tmp_path / "run", "-k", select)
    assert code == 1, records
    answers = [c["ok"] for c in records[0]["checks"] if c["name"] == "answer_only"]
    assert answers == [True, False]
