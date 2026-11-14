"""The context cases against a fleet that can drop conversation content behind a clean 200."""

from __future__ import annotations

import json

import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from llmdv.profile.schema import MESSAGES_CHECKS
from tests.fakes import FakeFleet, thinking_blocks
from tests.test_suites_against_fake import run_suites, write_profile

FOLLOWED = "anthropic_messages.mid_conversation_system_followed"
IN_PROMPT = "anthropic_messages.mid_conversation_system_in_prompt"
THINKING = "anthropic_messages.thinking_retained"
COUNT = "anthropic_messages.token_count_matches_usage"
TOOL_THINKING = "anthropic_messages.thinking_survives_tool_result"
SELECT = "test_conversation"
# A fleet that always answers with fixed thinking blocks cannot also echo a marker.
THINKING_FLEET = "test_earlier_thinking or test_token_count_matches"


# The cases send a few hundred filler words; the default fake context is smaller than that.
CONTEXT = 4096


def fake(**options):
    return FakeFleet(max_model_len=CONTEXT, **options)


def declare(tmp_path, fleet, **capabilities):
    path = write_profile(tmp_path, fleet, max_model_len=CONTEXT)
    raw = yaml.safe_load(path.read_text())
    checks = [name for name in MESSAGES_CHECKS if capabilities.pop(name, False)]
    raw["capabilities"].update(capabilities, messages_checks=checks)
    path.write_text(yaml.safe_dump(raw))
    return path


def run(tmp_path, fleet, select=SELECT, **capabilities):
    capabilities = {
        "mid_conversation_system": True,
        "prompt_accounting": True,
        "messages_thinking": ["enabled"],
        **capabilities,
    }
    profile = declare(tmp_path, fleet, **capabilities)
    code, records = run_suites(profile, tmp_path / "run", "-k", select, "--llmdv-evidence", "all")
    return code, {r["case_id"]: r for r in records if r.get("case_id")}


def failed_check(record, name):
    return any(c["name"] == name and not c["ok"] for c in record["checks"])


def test_healthy_fleet_passes_every_context_case_with_recorded_checks(tmp_path):
    with fake(prompt_accounting=True, message_overrides={"content": thinking_blocks()}) as fleet:
        code, by_case = run(tmp_path / "thinking", fleet, THINKING_FLEET)
    assert code == 0, by_case
    assert by_case[THINKING]["outcome"] == by_case[COUNT]["outcome"] == "pass"
    with fake(prompt_accounting=True) as fleet:
        code, plain = run(tmp_path / "plain", fleet, messages_thinking=[])
    assert code == 0, plain
    assert plain[FOLLOWED]["outcome"] == plain[IN_PROMPT]["outcome"] == "pass"
    assert plain[THINKING]["outcome"] == "skip"
    for record in (by_case[THINKING], by_case[COUNT], plain[FOLLOWED], plain[IN_PROMPT]):
        assert record["checks"] and all(check["ok"] for check in record["checks"])


def test_mid_conversation_system_request_is_sent_as_a_system_role_entry(tmp_path):
    with fake(prompt_accounting=True) as fleet:
        run(tmp_path, fleet, messages_thinking=[])
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    bodies = [json.loads(line) for line in lines]
    (followed,) = [b["request_body"] for b in bodies if b["case_id"] == FOLLOWED]
    roles = [m["role"] for m in followed["messages"]]
    assert roles == ["user", "assistant", "system", "user"]
    without, with_note = [b["request_body"] for b in bodies if b["case_id"] == IN_PROMPT]
    assert [m["role"] for m in without["messages"]] == ["user", "assistant", "user"]
    assert [m["role"] for m in with_note["messages"]] == roles


@pytest.mark.parametrize(
    "defect,case,check",
    [
        ("drops_mid_system", FOLLOWED, "mid_conversation_system_followed"),
        ("ignores_mid_system", FOLLOWED, "mid_conversation_system_followed"),
        ("drops_mid_system", IN_PROMPT, "mid_conversation_system_tokens"),
    ],
)
def test_lost_mid_conversation_system_fails_on_http_200(tmp_path, defect, case, check):
    with fake(prompt_accounting=True, context_defect=defect) as fleet:
        code, by_case = run(tmp_path, fleet, messages_thinking=[])
    assert code == 1
    record = by_case[case]
    assert record["outcome"] == "fail" and record["http_status"] == 200
    assert failed_check(record, check)


def test_a_layer_that_delivers_but_ignores_the_entry_keeps_its_prompt_size(tmp_path):
    """Delivered as text the model disregards: only the answer case can see it."""
    with fake(prompt_accounting=True, context_defect="ignores_mid_system") as fleet:
        _, by_case = run(tmp_path, fleet, messages_thinking=[])
    assert by_case[IN_PROMPT]["outcome"] == "pass"
    assert by_case[FOLLOWED]["outcome"] == "fail"


def test_dropped_thinking_fails_with_both_sizes_recorded(tmp_path):
    with fake(
        prompt_accounting=True,
        context_defect="drops_thinking",
        message_overrides={"content": thinking_blocks()},
    ) as fleet:
        code, by_case = run(tmp_path, fleet, THINKING_FLEET)
    assert code == 1
    record = by_case[THINKING]
    assert record["outcome"] == "fail"
    (check,) = [c for c in record["checks"] if c["name"] == "thinking_retained_tokens"]
    assert not check["ok"] and check["observed"]["grew"] == 0


def test_thinking_case_sends_the_signed_block_back_unchanged(tmp_path):
    with fake(prompt_accounting=True, message_overrides={"content": thinking_blocks()}) as fleet:
        run(tmp_path, fleet, THINKING_FLEET)
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    bodies = [json.loads(line)["request_body"] for line in lines]
    sent = [b for b in bodies if len(b["messages"]) == 3 and "thinking" in b]
    without, with_thinking = [b["messages"][1]["content"] for b in sent]
    assert with_thinking == thinking_blocks()
    assert without == [b for b in thinking_blocks() if b["type"] != "thinking"]


def test_token_count_that_ignores_part_of_the_prompt_fails(tmp_path):
    with fake(prompt_accounting=True, context_defect="count_ignores_system") as fleet:
        code, by_case = run(tmp_path, fleet, messages_thinking=[])
    assert code == 1
    record = by_case[COUNT]
    assert record["outcome"] == "fail"
    assert failed_check(record, "count_tokens_matches_usage")


def test_undeclared_capabilities_skip_without_sending_requests(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", SELECT)
        assert not [s for s in fleet.seen if s[0] == "POST"]
    assert code == 0, records
    assert {r["outcome"] for r in records} == {"skip"}
    assert {r["skip_class"] for r in records} == {"undeclared"}


def test_fixed_input_tokens_cannot_pass_the_size_cases(tmp_path):
    """A fleet whose usage does not track the prompt must fail once it declares that it does."""
    with fake() as fleet:
        code, by_case = run(tmp_path, fleet, messages_thinking=[])
    assert code == 1
    assert by_case[IN_PROMPT]["outcome"] == "fail"
    assert by_case[COUNT]["outcome"] == "fail"


def run_tool_case(tmp_path, fleet, tools=True, thinking=("enabled",)):
    profile = declare(tmp_path, fleet, prompt_accounting=True, messages_thinking=list(thinking))
    raw = yaml.safe_load(profile.read_text())
    if tools:
        raw["model"]["tools"] = {"parser": "example"}
    profile.write_text(yaml.safe_dump(raw))
    code, records = run_suites(
        profile,
        tmp_path / "run",
        "-k",
        "test_thinking_before_a_tool_call",
        "--llmdv-evidence",
        "all",
    )
    return code, records


def test_thinking_beside_a_tool_call_is_compared_with_and_without_it(tmp_path):
    with fake(prompt_accounting=True, message_tools=True) as fleet:
        code, (record,) = run_tool_case(tmp_path, fleet)
    assert code == 0, record
    assert record["case_id"] == TOOL_THINKING and record["outcome"] == "pass"
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    first, without, with_thinking = [json.loads(line)["request_body"] for line in lines]
    assert "tool_result" not in json.dumps(first)
    kinds = [
        [b["type"] for b in body["messages"][-2]["content"]] for body in (without, with_thinking)
    ]
    assert kinds == [["text", "tool_use"], ["thinking", "text", "tool_use"]]
    assert without["messages"][-1] == with_thinking["messages"][-1]


def test_thinking_dropped_before_the_tool_result_turn_fails(tmp_path):
    with fake(prompt_accounting=True, message_tools=True, context_defect="drops_thinking") as fleet:
        code, (record,) = run_tool_case(tmp_path, fleet)
    assert code == 1
    assert record["outcome"] == "fail" and record["http_status"] == 200
    assert failed_check(record, "thinking_survives_tool_result_tokens")


def test_tool_thinking_case_skips_without_a_declared_tool_parser(tmp_path):
    with fake(prompt_accounting=True, message_tools=True) as fleet:
        code, (record,) = run_tool_case(tmp_path, fleet, tools=False)
    assert code == 0
    assert record["outcome"] == "skip" and record["skip_class"] == "undeclared"


def test_tool_thinking_case_asks_for_thinking_and_does_not_force_the_call(tmp_path):
    with fake(prompt_accounting=True, message_tools=True) as fleet:
        run_tool_case(tmp_path, fleet)
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    first = json.loads(lines[0])["request_body"]
    assert first["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert first["tool_choice"] == {"type": "auto"}
    assert first["max_tokens"] > first["thinking"]["budget_tokens"]


def test_tool_thinking_case_skips_without_a_declared_thinking_mode(tmp_path):
    with fake(prompt_accounting=True, message_tools=True) as fleet:
        code, (record,) = run_tool_case(tmp_path, fleet, thinking=())
        assert not [s for s in fleet.seen if s[0] == "POST"]
    assert code == 0
    assert record["outcome"] == "skip" and record["skip_class"] == "undeclared"


def test_an_unknown_messages_check_is_a_profile_error(tmp_path):
    with fake() as fleet:
        path = declare(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"]["messages_checks"] = ["prompt_acounting"]
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ProfileError, match="messages_checks"):
        load(path)
