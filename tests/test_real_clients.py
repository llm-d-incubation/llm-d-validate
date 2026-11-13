import json
import os
import signal
import subprocess
import sys
import time

import pytest
import yaml

from llmdv.clients import process as process_module
from llmdv.clients.claude_code import isolated_env
from llmdv.clients.process import run_process
from tests.fakes import MODEL, FakeFleet, fake_claude_binary
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize("defect", [None, "error", "empty", "wrong_model", "retry"])
def test_real_client_harness_against_executable_fake(tmp_path, defect):
    binary = fake_claude_binary(tmp_path / "claude", defect=defect)
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["clients"] = [
            {
                "id": "claude-code",
                "surfaces": ["messages"],
                "binaries": [{"version": "2.1.0", "executable": str(binary)}],
            }
        ]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_claude_code_smoke")
        assert len(fleet.seen) == 1
    assert code == (0 if defect is None else 1), records
    assert records[0]["outcome"] == ("pass" if defect is None else "fail"), records
    assert records[0]["variant"] == "claude-code@2.1.0"


def test_version_axis_keeps_separate_evidence(tmp_path):
    binaries = [
        {"version": version, "executable": str(fake_claude_binary(tmp_path / version, version))}
        for version in ("2.1.0", "2.1.1")
    ]
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["clients"] = [{"id": "cli", "surfaces": ["messages"], "binaries": binaries}]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path, tmp_path / "run", "-k", "test_claude_code_smoke", "--llmdv-evidence", "all"
        )
    assert code == 0, records
    assert {r["variant"] for r in records} == {"cli@2.1.0", "cli@2.1.1"}
    assert len(list((tmp_path / "run" / "cases" / "fake").iterdir())) == 2


def test_environment_excludes_ambient_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "private-token")
    monkeypatch.setenv("NODE_OPTIONS", "--require danger.js")
    env = isolated_env(tmp_path, "http://127.0.0.1:1", "model")
    assert "private-key" not in json.dumps(env)
    assert "private-token" not in json.dumps(env)
    assert "NODE_OPTIONS" not in env
    assert os.environ["ANTHROPIC_API_KEY"] == "private-key"


@pytest.mark.parametrize(
    "source,problem,timeout",
    [
        ("import time; time.sleep(30)", "timeout", 0.5),
        # The cap trips on the first read, however long the interpreter took to start; a short
        # timeout here raced interpreter start-up on a loaded machine and reported a timeout.
        ("import sys; sys.stdout.write('x' * 50000)", "cap", 30),
    ],
)
def test_process_is_bounded(tmp_path, source, problem, timeout):
    result = run_process(
        [sys.executable, "-c", source], cwd=tmp_path, env={}, timeout=timeout, cap=1000
    )
    assert problem in result.problem, result
    assert len(result.stdout) <= 1000


def _wait_until_exited(pid: int) -> None:
    """Block until `pid` is a zombie: exited but not yet reaped, so its group has no live member.

    The cap verdict stops reading without waiting, so the child is never reaped before the kill.
    """
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
        if state.startswith("Z"):
            return
        time.sleep(0.01)
    raise AssertionError(f"child {pid} did not exit")


def _assert_reaped(pid: int) -> None:
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_process_survives_killing_a_group_that_already_exited(tmp_path, monkeypatch):
    """The leader exits before the cleanup kill: on macOS killpg then answers EPERM.

    Delaying the kill until the child is a zombie makes the race that flaked the cap case
    deterministic. Linux signals a zombie group successfully, so there this passes either way.
    """
    real_killpg = os.killpg
    killed: list[int] = []

    def late_killpg(pgid: int, sig: int) -> None:
        killed.append(pgid)
        _wait_until_exited(pgid)
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", late_killpg)
    result = run_process(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 50000)"],
        cwd=tmp_path,
        env={},
        timeout=30,
        cap=1000,
    )
    assert result.problem == "client output exceeded evidence cap", result
    _assert_reaped(killed[0])


def test_process_treats_eperm_from_an_exited_group_as_nothing_left_to_kill(tmp_path, monkeypatch):
    """Platform-independent: the kill is refused after the child exited; the verdict stands."""
    killed: list[int] = []

    def refused(pgid: int, sig: int) -> None:
        killed.append(pgid)
        _wait_until_exited(pgid)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", refused)
    result = run_process(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 50000)"],
        cwd=tmp_path,
        env={},
        timeout=30,
        cap=1000,
    )
    assert result.problem == "client output exceeded evidence cap", result
    assert result.code == 0, result
    _assert_reaped(killed[0])


def test_process_waits_for_a_leader_that_refused_the_kill_while_exiting(tmp_path, monkeypatch):
    """macOS also refuses the kill while the leader is part-way through exiting, before it can
    be reaped (measured: reapable within a tenth of a second), so poll() still reports it
    running. Here the refused leader has 0.2 s left to run, standing in for that window."""
    killed: list[int] = []

    def refused(pgid: int, sig: int) -> None:
        killed.append(pgid)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", refused)
    result = run_process(
        [sys.executable, "-c", "import sys, time; sys.stdout.write('x' * 50000); time.sleep(0.2)"],
        cwd=tmp_path,
        env={},
        timeout=30,
        cap=1000,
    )
    assert result.problem == "client output exceeded evidence cap", result
    _assert_reaped(killed[0])


def test_process_still_raises_when_a_running_group_cannot_be_killed(tmp_path, monkeypatch):
    """EPERM is only forgiven once the leader has exited; a live child must not be ignored."""
    killed: list[int] = []

    def refused(pgid: int, sig: int) -> None:
        killed.append(pgid)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", refused)
    monkeypatch.setattr(process_module, "EXIT_GRACE_SECONDS", 0.2)
    try:
        with pytest.raises(PermissionError):
            run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                env={},
                timeout=0.5,
            )
    finally:
        monkeypatch.undo()
        for pid in killed:
            os.killpg(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.mark.parametrize(
    "defect,abort,expected", [("stream", False, 0), ("stream", True, 1), ("output_limit", False, 1)]
)
def test_native_stream_and_limits_are_checked(tmp_path, defect, abort, expected):
    binary = fake_claude_binary(tmp_path / "claude", defect=defect)
    with FakeFleet(abort_finish_reason=abort) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["clients"] = [
            {
                "id": "cli",
                "surfaces": ["messages"],
                "binaries": [{"version": "2.1.0", "executable": str(binary)}],
            }
        ]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_claude_code_smoke")
        if defect == "output_limit":
            assert not fleet.seen
    assert code == expected, records
    assert records[0]["outcome"] == ("pass" if expected == 0 else "fail"), records


def test_version_mismatch_stops_before_inference(tmp_path):
    binary = fake_claude_binary(tmp_path / "claude", version="2.1.1")
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["clients"] = [
            {
                "id": "cli",
                "surfaces": ["messages"],
                "binaries": [{"version": "2.1.0", "executable": str(binary)}],
            }
        ]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_claude_code_smoke")
        assert not fleet.seen
    assert code == 1 and "differs from pin" in records[0]["reason"], records


def _run_smoke(tmp_path, fleet, binary):
    path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["clients"] = [
        {
            "id": "cli",
            "surfaces": ["messages"],
            "binaries": [{"version": "2.1.0", "executable": str(binary)}],
        }
    ]
    path.write_text(yaml.safe_dump(raw))
    return run_suites(path, tmp_path / "run", "-k", "test_claude_code_smoke")


def _check(record, name):
    return next(c for c in record["checks"] if c["name"] == name)


@pytest.mark.parametrize("status", [404, 500])
def test_a_failed_generation_is_blamed_on_its_status_not_the_refused_retry(tmp_path, status):
    """The client repeats a failed generation; the relay refuses it, and the verdict says why.

    Claude Code falls back from streaming to non-streaming after an error, which is the same
    generation again. The relay is right to refuse it, but a case that checks the client
    process first reports the refusal (or the client's exit) and hides the status that caused
    it.
    """
    binary = fake_claude_binary(tmp_path / "claude", defect="fallback")
    with FakeFleet(model_errors={MODEL: status}) as fleet:
        code, records = _run_smoke(tmp_path, fleet, binary)
        assert len(fleet.seen) == 1
    record = records[0]
    assert code == 1 and record["outcome"] == "fail", records
    assert record["reason"].startswith("AssertionError: client_upstream_statuses"), record
    assert f"{status}: 1" in record["reason"] and "refused" in record["reason"], record
    attempts = _check(record, "client_generation_attempts")
    assert not attempts["ok"] and "extra generation attempt" in str(attempts["observed"])
    assert f"upstream {status}" in str(attempts["observed"]), attempts
    assert not any(c["name"] == "client_process" for c in record["checks"]), record


def test_a_client_that_streams_first_makes_one_generation_on_a_healthy_fleet(tmp_path):
    binary = fake_claude_binary(tmp_path / "claude", defect="fallback")
    with FakeFleet() as fleet:
        code, records = _run_smoke(tmp_path, fleet, binary)
    assert code == 0, records
    attempts = _check(records[0], "client_generation_attempts")
    assert attempts["ok"], attempts
    assert "'attempts': 1" in str(attempts["observed"]) and "'rejected': []" in str(
        attempts["observed"]
    )
