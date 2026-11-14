import json

import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from tests.fakes import FakeFleet, chat_reasoning_payload
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize(
    "carrier,field,passes",
    [
        ("inline", None, True),
        ("reasoning_content", "reasoning_content", True),
        ("missing", None, False),
        ("reasoning", "reasoning_content", False),
    ],
)
def test_declared_shape(tmp_path, carrier, field, passes):
    with FakeFleet(chat_overrides=chat_reasoning_payload(carrier)) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        if field:
            raw["model"]["reasoning"] = {"parser": "example", "field": field, "default_on": True}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path, tmp_path / "run", "-k", "test_configured_reasoning and json"
        )
    assert code == (0 if passes else 1), records
    assert any(
        c["name"] == "chat_reasoning_shape" and c["ok"] is passes
        for r in records
        for c in r["checks"]
    )
    if passes:
        assert answer_only(records[0])["ok"]


def answer_only(record):
    return next(c for c in record["checks"] if c["name"] == "answer_only")


def declare_field(path, **capabilities):
    raw = yaml.safe_load(path.read_text())
    raw["model"]["reasoning"] = {"parser": "example", "field": "reasoning_content"}
    raw["model"]["reasoning"]["default_on"] = True
    raw["capabilities"].update(capabilities)
    path.write_text(yaml.safe_dump(raw))


@pytest.mark.parametrize("leak", ["glued", "unopened", "untagged", "swallowed"])
def test_reasoning_leaked_into_content_fails(tmp_path, leak):
    """Reasoning in its declared field, yet more of it in content: the answer is not alone."""
    with FakeFleet(chat_overrides=chat_reasoning_payload("reasoning_content", leak)) as fleet:
        path = write_profile(tmp_path, fleet)
        declare_field(path)
        code, records = run_suites(
            path, tmp_path / "run", "-k", "test_configured_reasoning and json"
        )
    assert code == 1, records
    assert next(c for c in records[0]["checks"] if c["name"] == "chat_reasoning_shape")["ok"]
    assert not answer_only(records[0])["ok"]


@pytest.mark.parametrize("leak", [None, "unopened", "untagged", "swallowed"])
def test_streamed_answer_is_judged_after_assembly(tmp_path, leak):
    with FakeFleet(answer_leak=leak) as fleet:
        path = write_profile(tmp_path, fleet)
        code, records = run_suites(
            path, tmp_path / "run", "-k", "test_configured_reasoning and sse"
        )
        request = (
            fleet.seen
            and json.loads((tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()[0])[
                "request_body"
            ]
        )
    assert code == (0 if leak is None else 1), records
    assert request["stream"] and request["stream_options"] == {"include_usage": True}
    assert request["messages"][0]["content"] == "What is 17 times 23? Reply with the number only."
    assert answer_only(records[0])["ok"] is (leak is None)


@pytest.mark.parametrize(
    "effort,carrier,leak,passes",
    [
        ("none", "missing", None, True),
        ("none", "missing", "untagged", False),
        ("high", "reasoning_content", None, True),
        ("high", "reasoning_content", "untagged", False),
        ("high", "reasoning_content", "unopened", False),
    ],
)
def test_every_effort_answers_alone(tmp_path, effort, carrier, leak, passes):
    with FakeFleet(chat_overrides=chat_reasoning_payload(carrier, leak)) as fleet:
        path = write_profile(tmp_path, fleet)
        declare_field(path, chat_reasoning_efforts=[effort])
        code, records = run_suites(path, tmp_path / "run", "-k", "test_reasoning_effort")
    assert code == (0 if passes else 1), records
    (record,) = [r for r in records if r["outcome"] != "skip"]
    assert next(c for c in record["checks"] if c["name"] == "chat_reasoning_shape")["ok"]
    assert answer_only(record)["ok"] is passes


@pytest.mark.parametrize("carrier,passes", [("missing", True), ("inline", False)])
def test_none_cannot_be_ignored(tmp_path, carrier, passes):
    with FakeFleet(chat_overrides=chat_reasoning_payload(carrier)) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"]["chat_reasoning_efforts"] = ["none"]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_reasoning_effort")
        assert len(fleet.seen) == 1
        exchange = json.loads((tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()[0])
        assert exchange["request_body"]["reasoning_effort"] == "none"
    assert code == (0 if passes else 1), records


@pytest.mark.parametrize("value", ["low", ["typo"], ["low", "low"], [True]])
def test_bad_efforts_fail_loading(tmp_path, value):
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump({"capabilities": {"chat_reasoning_efforts": value}}))
    with pytest.raises(ProfileError, match="chat_reasoning_efforts"):
        load(path)
