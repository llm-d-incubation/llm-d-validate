"""The session cases against an executable stand-in that can lose one behaviour at a time."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from llmdv.clients import claude_code
from llmdv.clients.process import ProcessResult
from llmdv.clients.relay import ClientRelay
from llmdv.evidence import journal
from llmdv.http import sse
from llmdv.http.client import Exchange
from llmdv.profile.schema import ClientBinary
from llmdv.suites.real_clients.helpers import (
    SESSION_TIMEOUT,
    check_process,
    check_upstream,
    session_timeout,
)
from llmdv.suites.real_clients.sessions import (
    PROJECT_FILE_CAP,
    PROJECT_TOTAL_CAP,
    _snapshot,
    compact_window,
)
from tests.fakes import MODEL, FakeFleet, fake_claude_session_binary
from tests.test_suites_against_fake import run_suites, write_profile

CASES = {
    "skill": ("real_clients.claude_code_skill", "skill_invoked"),
    "hook": ("real_clients.claude_code_hook", "hook_output_used"),
    "parallel_reads": ("real_clients.claude_code_parallel_reads", "parallel_reads"),
    "nested_subagent": ("real_clients.claude_code_nested_subagent", "nested_subagent"),
    "compaction": ("real_clients.claude_code_compaction", "compaction_recall"),
    "multi_file_fix": ("real_clients.claude_code_multi_file_fix", "multi_file_fix"),
    "image_read": ("real_clients.claude_code_image_read", "image_content_read"),
}


def run(tmp_path, fleet, binary, sessions, select="test_session"):
    path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"]["client_sessions"] = sessions
    raw["clients"] = [
        {
            "id": "cli",
            "surfaces": ["messages"],
            "binaries": [{"version": "2.1.154", "executable": str(binary)}],
        }
    ]
    path.write_text(yaml.safe_dump(raw))
    code, records = run_suites(path, tmp_path / "run", "-k", select, "--llmdv-evidence", "all")
    return code, {r["case_id"]: r for r in records if r.get("case_id")}


def test_every_declared_session_passes_with_a_working_client(tmp_path):
    binary = fake_claude_session_binary(tmp_path / "claude")
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, binary, list(CASES))
        assert len([s for s in fleet.seen if s[0] == "POST"]) == len(CASES)
    assert code == 0, by_case
    for case, check in CASES.values():
        record = by_case[case]
        assert record["outcome"] == "pass", record
        assert record["variant"] == "cli@2.1.154"
        assert any(c["name"] == check and c["ok"] for c in record["checks"])


@pytest.mark.parametrize(
    "session,defect",
    [
        ("skill", "no_skill"),
        ("hook", "no_hook"),
        ("parallel_reads", "serial_reads"),
        ("nested_subagent", "flat_subagent"),
        # `forgets` compacts and then loses five of the six codes, which is the behaviour this
        # case exists to catch. `no_compaction` -- the client never crossing its window -- is a
        # precondition that was not met, and skips; see the test below.
        ("compaction", "forgets"),
        ("multi_file_fix", "unfixed"),
        ("multi_file_fix", "edits_tests"),
        ("image_read", "blind"),
    ],
)
def test_a_session_that_loses_the_behaviour_fails_its_case(tmp_path, session, defect):
    binary = fake_claude_session_binary(tmp_path / "claude", defect=defect)
    case, check = CASES[session]
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, binary, [session])
    assert code == 1
    record = by_case[case]
    assert record["outcome"] == "fail"
    assert any(c["name"] == check and not c["ok"] for c in record["checks"])
    others = [r for c, r in by_case.items() if c != case]
    assert others and all(r["skip_class"] == "undeclared" for r in others)


def test_a_session_that_never_compacted_skips_rather_than_failing(tmp_path):
    """Compaction is the precondition, not the behaviour under test.

    Whether the client crosses its window depends on the model's verbosity and the client
    version. A session that read everything and lost nothing has demonstrated nothing about
    recall across a compaction, which is a `requires_load` skip, not a fleet fault.
    """
    binary = fake_claude_session_binary(tmp_path / "claude", defect="no_compaction")
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, binary, ["compaction"])
    record = by_case[CASES["compaction"][0]]
    assert record["outcome"] == "skip", record
    assert record["skip_class"] == "requires_load", record
    assert code == 0


def test_a_session_ending_in_error_fails_before_its_feature_check(tmp_path):
    binary = fake_claude_session_binary(tmp_path / "claude", defect="error")
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, binary, ["skill"])
    assert code == 1
    record = by_case[CASES["skill"][0]]
    assert record["outcome"] == "fail"
    assert any(c["name"] == "client_session" and not c["ok"] for c in record["checks"])


# Every session that fails before its feature check when the fleet fails the generation. The
# compaction and multi-file sessions need the same relayed generation, so they are no different.
UPSTREAM_SESSIONS = [
    "hook",
    "skill",
    "compaction",
    "image_read",
    "parallel_reads",
    "nested_subagent",
]


@pytest.mark.parametrize("status", [404, 500])
def test_a_session_the_fleet_failed_is_blamed_on_the_status(tmp_path, status):
    """Upstream first: the client's exit and its event stream are the symptom, not the fault.

    The stand-in repeats the failed generation without streaming, as Claude Code does, and a
    session's bounds forward both. The verdict must name the status, not the client's exit.
    """
    binary = fake_claude_session_binary(tmp_path / "claude", defect="fallback")
    with FakeFleet(model_errors={MODEL: status}) as fleet:
        code, by_case = run(tmp_path, fleet, binary, UPSTREAM_SESSIONS)
    assert code == 1
    for session in UPSTREAM_SESSIONS:
        record = by_case[CASES[session][0]]
        assert record["outcome"] == "fail", record
        assert record["reason"].startswith("AssertionError: session_upstream_statuses"), record
        assert f"{status}: 2" in record["reason"], record
        names = [c["name"] for c in record["checks"]]
        assert "relay_refusals" in names, names
        assert "client_process" not in names, names


def test_a_session_that_streams_first_makes_one_generation_on_a_healthy_fleet(tmp_path):
    binary = fake_claude_session_binary(tmp_path / "claude", defect="fallback")
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, binary, ["skill"])
    assert code == 0, by_case
    checks = {c["name"]: c for c in by_case[CASES["skill"][0]]["checks"]}
    assert checks["relay_refusals"]["observed"] == {}, checks
    assert checks["session_upstream_statuses"]["ok"], checks
    assert "'generations': 1" in str(checks["client_session"]["observed"]), checks


def test_undeclared_sessions_send_nothing(tmp_path):
    binary = fake_claude_session_binary(tmp_path / "claude")
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, binary, [])
        assert not fleet.seen
    assert code == 0
    assert {r["skip_class"] for r in by_case.values()} == {"undeclared"}


def test_session_names_are_validated_at_load(tmp_path):
    from llmdv.profile.loader import ProfileError, load

    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"capabilities": {"client_sessions": ["typo"]}}))
    with pytest.raises(ProfileError, match="client_sessions"):
        load(path)


def test_relay_limits_default_to_the_probe_bounds():
    relay = ClientRelay(client=None)
    assert relay.max_generations == 1


def test_calls_without_a_message_id_are_not_counted_as_one_turn():
    from pathlib import Path

    from llmdv.suites.real_clients.sessions import Session

    def read(message_id):
        block = {"type": "tool_use", "name": "Read", "input": {}}
        return {"type": "assistant", "message": {"id": message_id, "content": [block]}}

    apart = Session(Path("."), [read(None), read(None), read(None)], "")
    together = Session(Path("."), [read("m1"), read("m1"), read("m2")], "")
    assert apart.most_calls_in_one_turn("Read") == 1
    assert together.most_calls_in_one_turn("Read") == 2


def test_hook_value_is_in_no_project_file():
    from llmdv.suites.real_clients import projects

    files = projects.hook_project()
    assert projects.HOOK_VARIABLE in files[".claude/settings.json"]
    assert all(len(text) < 400 for text in files.values())


def test_session_evidence_records_what_the_relay_refused(tmp_path):
    binary = fake_claude_session_binary(tmp_path / "claude")
    with FakeFleet() as fleet:
        _, by_case = run(tmp_path, fleet, binary, ["skill"])
    record = by_case[CASES["skill"][0]]
    assert any(c["name"] == "relay_refusals" for c in record["checks"])


def _session_args(allowed_tools, **kwargs):
    """The argv invoke_session would run, without running it."""
    seen = {}

    def capture(args, **rest):
        seen["args"] = args
        return ProcessResult(code=0, stdout="", stderr="", problem=None)

    with patch.object(claude_code, "run_process", capture):
        claude_code.invoke_session(
            ClientBinary(version="2.1.277", executable=Path("/bin/true")),
            project=Path("/tmp"),
            env={"ANTHROPIC_MODEL": "m"},
            prompt="hi",
            timeout=1,
            allowed_tools=allowed_tools,
            **kwargs,
        )
    return seen["args"]


def test_file_writes_are_granted_only_inside_the_project():
    args = _session_args(("Read", "Edit", "Write"))
    granted = args[args.index("--allowedTools") + 1 :]
    assert claude_code.PROJECT_WRITES in granted
    # A bare Edit or Write would grant the whole filesystem.
    assert "Edit" not in granted and "Write" not in granted
    assert "Read" in granted


def test_the_project_settings_directory_is_denied():
    # .claude/settings.json defines hooks, which are shell commands, and --setting-sources
    # project reads it. A session that could rewrite it could grant itself a command.
    args = _session_args(("Read", "Edit", "Write"))
    index = args.index("--disallowedTools")
    assert args[index + 1] == claude_code.PROJECT_SETTINGS
    assert index < args.index("--allowedTools")


def test_every_allowed_tool_is_also_defined():
    args = _session_args(("Read", "Edit", "Bash(python3 -m unittest)"))
    defined = args[args.index("--tools") + 1 : args.index("--disallowedTools")]
    # --tools takes bare names; the permission patterns go to --allowedTools.
    assert defined == ["Read", "Edit", "Bash"]


def test_the_full_tool_set_stays_defined_when_restriction_is_off():
    args = _session_args(("Read",), restrict_tools=False)
    assert "--tools" not in args
    assert args[args.index("--allowedTools") + 1 :] == ["Read"]


def _relay_with(*exchanges):
    """A finished relay carrying the generations it forwarded, and nothing else."""
    relay = SimpleNamespace(generations=[(exchange, None) for exchange in exchanges], rejected=[])
    return relay


def _ingress(rate_limited=False):
    return SimpleNamespace(id="router", rate_limited=rate_limited)


def _exchange(status=200, transport_error=None, headers=None):
    return Exchange(
        method="POST",
        url="http://relay/v1/messages",
        status=status,
        transport_error=transport_error,
        response_headers=headers or {},
    )


def test_a_session_whose_generations_all_succeeded_is_accepted():
    check_upstream(_relay_with(_exchange(), _exchange(), _exchange()), _ingress(), "upstream")


def test_a_session_that_retried_past_a_server_error_fails():
    # The client's own result is successful; the fleet was not. Without this the session passes.
    with pytest.raises(AssertionError, match="every generation served 200"):
        check_upstream(_relay_with(_exchange(500), _exchange()), _ingress(), "upstream")


def test_a_session_that_retried_past_a_transport_failure_fails():
    with pytest.raises(AssertionError, match="every generation served 200"):
        check_upstream(
            _relay_with(_exchange(None, "connection reset"), _exchange()), _ingress(), "upstream"
        )


def test_a_declared_throttle_skips_the_session_rather_than_failing_it():
    # A small quota is how a suite is made safe to point at a shared fleet; the 429 is the
    # limit working, so it must not read as a fleet fault.
    with pytest.raises(BaseException) as caught:
        check_upstream(
            _relay_with(_exchange(429), _exchange()), _ingress(rate_limited=True), "upstream"
        )
    assert "throttled" in str(caught.value).lower()


def test_an_undeclared_throttle_still_fails():
    with pytest.raises(AssertionError, match="does not declare it rate limited"):
        check_upstream(_relay_with(_exchange(429)), _ingress(), "upstream")


def test_a_failing_process_check_does_not_carry_megabytes_into_results():
    """A check's payload goes to results.jsonl, which llmdv compare reads back for every run.

    A session reads up to 8MB of event trace, and this check used to carry all of it.
    """
    huge = "x" * (2 * 1024 * 1024)
    active = journal.activate("tests::check_process")
    try:
        with pytest.raises(AssertionError):
            check_process(
                ProcessResult(code=1, stdout=huge, stderr="", problem=None), "client_process"
            )
        observed = active.checks[-1].observed
    finally:
        journal.deactivate()
    # fail() stringifies its payload, so the clamp is what keeps the check -- and the assertion
    # message that goes with it -- from carrying the whole trace.
    payload = len(str(observed).encode("utf-8"))
    assert payload <= sse.DEFAULT_TRANSCRIPT_CAP + 4096, payload
    # The real size survives even though the text does not, and the verdict says it was cut.
    assert f"'stdout_bytes': {len(huge)}" in observed
    assert "'truncated': True" in observed


def test_the_relay_tells_a_denied_client_not_to_retry():
    """The SDK retries 408, 409, 429 and 5xx, so the status alone cannot end an attempt."""
    source = inspect.getsource(ClientRelay)
    assert '"x-should-retry": "false"' in source


def test_a_session_project_snapshot_is_bounded_and_lists_every_file(tmp_path):
    project = tmp_path / "project"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "small.py").write_text("print('ok')\n")
    (project / "pkg" / "big.py").write_text("y" * (PROJECT_FILE_CAP * 2))
    (project / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    text = _snapshot(project)
    assert "pkg/small.py" in text and "print('ok')" in text
    assert "pkg/big.py" in text and "[llmdv] truncated" in text
    assert "blob.bin" in text and "not read as UTF-8 text" in text
    assert len(text.encode("utf-8")) < PROJECT_TOTAL_CAP * 2


@pytest.mark.parametrize(("request_timeout", "expected"), [(60, 60), (600, SESSION_TIMEOUT)])
def test_the_session_timeout_never_exceeds_the_declared_request_timeout(request_timeout, expected):
    ingress = SimpleNamespace(timeouts=SimpleNamespace(request=request_timeout))
    assert session_timeout(ingress) == expected


def test_removing_the_thinking_cap_survives_an_absent_key():
    env = claude_code.isolated_env(Path("/tmp"), "http://relay", "m")
    del env["MAX_THINKING_TOKENS"]
    with patch.object(claude_code, "isolated_env", lambda *a, **k: env):
        # pop, not del: this used to raise KeyError.
        assert "MAX_THINKING_TOKENS" not in claude_code.session_env(Path("/tmp"), "u", "m")


@pytest.mark.parametrize(
    "context,expected",
    [(16_000, 8_000), (275_000, 40_000), (40_000, 20_000)],
    ids=["small-fleet", "large-fleet-capped", "mid"],
)
def test_the_compaction_window_never_exceeds_the_model_s_own_limit(context, expected):
    """A window at or above the model's context can never be reached: the limit binds first.

    A fixed 40000 is dead on any fleet with a smaller context, and the case has no way to say
    so -- it just never compacts.
    """
    model = SimpleNamespace(max_model_len=context)
    assert compact_window(model) == expected
    assert compact_window(model) < context


def test_a_failing_process_check_leads_with_the_client_s_result_event():
    """A session's stdout opens with a start-up event kilobytes long; its error is at the end.

    The reason a reader sees is the head of the check, so it must carry the result event.
    """
    init = {"type": "system", "subtype": "init", "tools": ["Read"] * 2000}
    ended = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "api_error_status": 404,
        "terminal_reason": "api_error",
        "result": "API Error: 404 model not found",
        "usage": {"input_tokens": 0},
    }
    stdout = json.dumps(init) + "\n" + json.dumps(ended) + "\n"
    with pytest.raises(AssertionError) as caught:
        check_process(
            ProcessResult(code=1, stdout=stdout, stderr="", problem=None), "client_process"
        )
    head = str(caught.value)[:400]
    assert "api_error_status" in head and "API Error: 404 model not found" in head, head
    assert "usage" not in head, head


def test_a_failing_process_without_a_result_event_says_so():
    with pytest.raises(AssertionError) as caught:
        check_process(
            ProcessResult(code=1, stdout="boom", stderr="", problem=None), "client_process"
        )
    assert "'result': None" in str(caught.value).replace("\\'", "'"), caught.value


def test_the_result_event_s_text_is_clamped():
    # Its result text carries the fleet's error body, which no reason needs whole.
    ended = {"type": "result", "is_error": True, "result": "x" * 100_000}
    with pytest.raises(AssertionError) as caught:
        check_process(
            ProcessResult(code=1, stdout=json.dumps(ended), stderr="", problem=None),
            "client_process",
        )
    head = str(caught.value).split("stdout")[0]
    assert "bytes dropped" in head and len(head) < 10_000, len(head)
