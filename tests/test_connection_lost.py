"""A local tunnel that dies mid-run is the environment's fault, not the fleet's.

Preflight already turns a dead port-forward into exit 3 before the first case. These cover the
same tunnel dying after preflight passed: the case that found it gone is an error naming the
lost connection, later cases on that ingress are skipped, and the run exits 3 unless a real
failure also happened. Nothing is retried and no tunnel is restarted.

The evidence has to be the operator's side: the local port itself refusing connections. A local
port that still accepts, whatever happens to the request afterwards, is a server or a pod
crashing behind a live tunnel, and that stays a fleet result.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from llmdv import plugin, preflight
from llmdv.evidence import journal
from llmdv.http.client import Exchange
from llmdv.profile import loader
from tests.fakes import SRC, FakeFleet, LocalForward, profile_yaml, run_llmdv

SELECTION = ("-m", "openai_chat")


def _profile(
    tmp_path: Path, fleet: FakeFleet, timeouts: str | None = None, base_url: str | None = None
) -> Path:
    text = profile_yaml(base_url or fleet.base_url, fleet.model)
    if timeouts:
        text = text.replace("    health_path: /health\n", f"    health_path: /health\n{timeouts}\n")
    path = tmp_path / "fake.yaml"
    path.write_text(text)
    return path


def _verify(tmp_path: Path, profile: Path, *, non_local: bool = False):
    args = [
        "verify", "--profile", str(profile), "--local-output", str(tmp_path / "run"),
        "--pod-logs", "off", *SELECTION,
    ]  # fmt: skip
    if not non_local:
        return run_llmdv(*args)
    # The fake only listens on loopback, so a non-local ingress is the same fake with the
    # predicate saying otherwise, in the child process that runs the suite.
    script = (
        "import sys; from llmdv import preflight; preflight.is_local = lambda ingress: False; "
        "from llmdv.cli.main import main; sys.exit(main(sys.argv[1:]))"
    )
    return _run_python(script, args)


def _run_python(script: str, args: list[str]):
    return subprocess.run(  # noqa: S603 - fixed interpreter and args
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        cwd=SRC.parent,
        env={"PYTHONPATH": str(SRC), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )


def _records(tmp_path: Path) -> list[dict]:
    lines = (tmp_path / "run" / "results.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _outcome(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "run" / "outcome.json").read_text())


def test_a_local_tunnel_lost_mid_run_exits_3_and_records_no_fleet_failure(tmp_path):
    with FakeFleet(go_dark_after=3) as fleet:
        result = _verify(tmp_path, _profile(tmp_path, fleet))
    assert result.returncode == 3, result.stderr

    records = _records(tmp_path)
    assert not [r for r in records if r["outcome"] == "fail"]
    [hit] = [r for r in records if r["outcome"] == "error"]
    assert hit["connection_lost"] == "fake"
    assert "local connection to ingress 'fake'" in hit["reason"]
    assert "not the fleet" in hit["reason"]

    index = records.index(hit)
    after = [r for r in records[index + 1 :] if r.get("connection_lost")]
    assert after, "later cases on the lost ingress should be skipped, not run"
    for record in after:
        assert record["outcome"] == "skip"
        assert record["skip_class"] == "requires_instrumentation"
        assert "local connection to ingress 'fake' was lost" in record["reason"]
    # Nothing after the loss ran against the ingress: every later record is a skip.
    assert {r["outcome"] for r in records[index + 1 :]} <= {"skip"}

    outcome = _outcome(tmp_path)
    assert outcome["verification"]["class"] == "environment"
    assert outcome["connection_lost"] == [
        {
            "ingress": "fake",
            "case_id": hit["case_id"],
            "variant": hit["variant"],
            "at": hit["ts"],
            "cases_before": index,
            "cases_skipped": len(after),
        }
    ]
    summary = (tmp_path / "run" / "summary.md").read_text()
    assert "## local connection lost" in summary
    said = result.stdout + result.stderr
    assert f"the local connection to ingress 'fake' was lost after {index} case(s)" in said
    assert "your environment, not the fleet" in said


def test_a_real_failure_before_the_loss_still_exits_1(tmp_path):
    with FakeFleet(abort_finish_reason=True, go_dark_after=4) as fleet:
        result = _verify(tmp_path, _profile(tmp_path, fleet))
    assert result.returncode == 1, result.stderr

    records = _records(tmp_path)
    assert [r for r in records if r["outcome"] == "fail" and not r.get("connection_lost")]
    # The loss is still reported; it just does not hide the fleet's failure.
    outcome = _outcome(tmp_path)
    assert outcome["verification"]["class"] == "fleet"
    assert [entry["ingress"] for entry in outcome["connection_lost"]] == ["fake"]


def test_a_non_local_ingress_losing_its_connection_stays_a_fleet_result(tmp_path):
    with FakeFleet(go_dark_after=3) as fleet:
        result = _verify(tmp_path, _profile(tmp_path, fleet), non_local=True)
    assert result.returncode == 1, result.stderr

    records = _records(tmp_path)
    assert [r for r in records if r["outcome"] in ("fail", "error")]
    assert not [r for r in records if r.get("connection_lost")]
    assert not [r for r in records if r.get("skip_class") == "requires_instrumentation"]
    assert _outcome(tmp_path)["connection_lost"] == []


def test_a_slow_local_ingress_is_not_a_lost_connection(tmp_path):
    # Every generation outlives the request timeout: a read timeout after the connection was
    # made, which says nothing about the tunnel. The failures stay the fleet's.
    with FakeFleet(delay_s=1.0) as fleet:
        timeouts = "    timeouts: {connect: 5, request: 0.3, stream_idle: 0.3}"
        result = _verify(tmp_path, _profile(tmp_path, fleet, timeouts))
    assert result.returncode == 1, result.stderr

    records = _records(tmp_path)
    assert [r for r in records if r["outcome"] in ("fail", "error")]
    assert not [r for r in records if r.get("connection_lost")]
    assert _outcome(tmp_path)["connection_lost"] == []


def test_a_local_server_that_crashes_and_recovers_is_a_fleet_failure(tmp_path):
    # The local port never stops accepting: the tunnel is alive, and what is behind it drops
    # one request, stays down for the next connections and comes back. The fleet crashed.
    with (
        FakeFleet() as fleet,
        LocalForward(fleet.base_url, crash_on_post=3, down_for=3) as forward,
    ):
        result = _verify(tmp_path, _profile(tmp_path, fleet, base_url=forward.base_url))
        assert forward.accepting
    assert result.returncode == 1, result.stderr

    records = _records(tmp_path)
    assert not [r for r in records if r.get("connection_lost")]
    assert not [r for r in records if r.get("skip_class") == "requires_instrumentation"]
    failed = [i for i, r in enumerate(records) if r["outcome"] in ("fail", "error")]
    assert failed and forward.dropped == 3
    # The ingress answered again, so the cases after the crash ran against it.
    assert [r for r in records[failed[-1] + 1 :] if r["outcome"] == "pass"]

    outcome = _outcome(tmp_path)
    assert outcome["verification"]["class"] == "fleet"
    assert outcome["connection_lost"] == []
    assert "your environment, not the fleet" not in result.stdout + result.stderr


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_a_local_listener_that_goes_away_exits_3(tmp_path, host):
    if host == "::1" and not _ipv6_loopback():
        pytest.skip("this machine has no IPv6 loopback")
    with FakeFleet() as fleet, LocalForward(fleet.base_url, host=host, close_on_post=3) as forward:
        result = _verify(tmp_path, _profile(tmp_path, fleet, base_url=forward.base_url))
        assert not forward.accepting
    assert result.returncode == 3, result.stderr

    records = _records(tmp_path)
    assert not [r for r in records if r["outcome"] == "fail"]
    [hit] = [r for r in records if r["outcome"] == "error"]
    assert hit["connection_lost"] == "fake"
    assert "refuses connections" in hit["reason"]
    assert {r["outcome"] for r in records[records.index(hit) + 1 :]} <= {"skip"}
    assert _outcome(tmp_path)["verification"]["class"] == "environment"


def _state(ingress, exchange: Exchange) -> tuple[SimpleNamespace, dict]:
    client = SimpleNamespace(exchanges=[exchange], last=exchange)
    config = SimpleNamespace(_llmdv_connection_lost={})
    return config, {"clients": [client], "client_ingresses": {id(client): ingress}}


def _reset() -> Exchange:
    return Exchange(
        "POST", "/v1/chat/completions", transport_error="ReadError: reset",
        connection_failed=True, failed_at=time.monotonic(),
    )  # fmt: skip


def _closed_ingress(tmp_path: Path):
    """An ingress on a loopback port nothing listens on any more."""
    with FakeFleet() as fleet:
        return loader.load(_profile(tmp_path, fleet)).ingress("fake")


def test_a_connection_error_on_a_local_port_that_still_accepts_stands(tmp_path):
    with FakeFleet() as fleet:
        ingress = loader.load(_profile(tmp_path, fleet)).ingress("fake")
        config, state = _state(ingress, _reset())
        assert plugin._connection_lost(config, state, None, ingress) is None


def test_a_failed_health_read_is_not_evidence_while_the_local_port_accepts(tmp_path):
    # Accepts every connection and closes it with nothing: no health answer, a live listener.
    with (
        FakeFleet() as fleet,
        LocalForward(fleet.base_url, crash_on_post=0, down_for=99) as forward,
    ):
        ingress = loader.load(_profile(tmp_path, fleet, base_url=forward.base_url)).ingress("fake")
        assert preflight.reachable(ingress)[0] is False
        config, state = _state(ingress, _reset())
        assert plugin._connection_lost(config, state, None, ingress) is None


def test_a_local_port_that_refuses_is_a_lost_connection(tmp_path):
    ingress = _closed_ingress(tmp_path)
    config, state = _state(ingress, _reset())
    owner, error = plugin._connection_lost(config, state, None, ingress)
    assert owner is ingress
    assert error == "ReadError: reset"


def test_a_port_that_accepts_again_within_the_window_is_not_lost(tmp_path, monkeypatch):
    # Refused once, then listening: a server restarting, not a tunnel that is gone.
    ingress = _closed_ingress(tmp_path)
    answers = iter([True, False])
    monkeypatch.setattr(preflight, "refuses_connections", lambda ingress: next(answers))
    monkeypatch.setattr(preflight.time, "sleep", lambda seconds: None)
    assert preflight.local_end_gone(ingress) is False
    config, state = _state(ingress, _reset())
    monkeypatch.setattr(preflight, "local_end_gone", lambda ingress: False)
    assert plugin._connection_lost(config, state, None, ingress) is None


def test_the_local_end_is_read_more_than_once_before_it_is_called_gone(tmp_path, monkeypatch):
    ingress = _closed_ingress(tmp_path)
    reads = []
    monkeypatch.setattr(preflight, "refuses_connections", lambda i: reads.append(i) or True)
    monkeypatch.setattr(preflight.time, "sleep", lambda seconds: None)
    assert preflight.local_end_gone(ingress) is True
    assert len(reads) == preflight.GONE_READS > 1


def test_a_check_that_failed_before_the_connection_did_keeps_the_case_a_fleet_result(tmp_path):
    # The case found a real defect, and only afterwards a request of its own met a dead port.
    ingress = _closed_ingress(tmp_path)
    active = journal.activate("case")
    try:
        journal.record("finish_reason", "abort", "stop", ok=False)
        time.sleep(0.01)
        config, state = _state(ingress, _reset())
        assert plugin._connection_lost(config, state, None, ingress, active.checks) is None
    finally:
        journal.deactivate()


def test_a_check_that_failed_on_the_lost_connection_itself_does_not_make_it_the_fleet(tmp_path):
    # Recorded after the request failed: it observed the missing response, not the fleet.
    ingress = _closed_ingress(tmp_path)
    active = journal.activate("case")
    try:
        config, state = _state(ingress, _reset())
        time.sleep(0.01)
        journal.record("status", None, "200", ok=False)
        assert plugin._connection_lost(config, state, None, ingress, active.checks) is not None
        assert "at" not in active.checks[0].to_dict()
    finally:
        journal.deactivate()


def test_a_lost_ingress_that_listens_again_runs_its_later_cases(tmp_path):
    with FakeFleet() as fleet:
        ingress = loader.load(_profile(tmp_path, fleet)).ingress("fake")
        port = int(fleet.base_url.rsplit(":", 1)[1])
    config = SimpleNamespace(_llmdv_connection_lost={"fake": {"case": "a.case"}})
    assert plugin._still_lost(config, ingress) is True
    assert "fake" in config._llmdv_connection_lost

    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        assert plugin._still_lost(config, ingress) is False
    assert config._llmdv_connection_lost == {}


def test_a_non_local_ingress_is_never_probed(tmp_path, monkeypatch):
    exchange = Exchange(
        "POST", "/v1/chat/completions", transport_error="ConnectError: refused",
        connection_failed=True,
    )  # fmt: skip
    probed = []
    monkeypatch.setattr(preflight, "is_local", lambda ingress: False)
    monkeypatch.setattr(preflight, "local_end_gone", lambda ingress: probed.append(ingress) or 1)
    with FakeFleet() as fleet:
        ingress = loader.load(_profile(tmp_path, fleet)).ingress("fake")
    config, state = _state(ingress, exchange)
    assert plugin._connection_lost(config, state, None, ingress) is None
    assert probed == []


def test_the_local_predicate_is_the_loopback_one_preflight_uses(tmp_path):
    with FakeFleet() as fleet:
        ingress = loader.load(_profile(tmp_path, fleet)).ingress("fake")
    assert preflight.is_local(ingress)
    for url in ("http://localhost:8000", "http://[::1]:8000", "https://[::1]"):
        assert preflight.is_local(SimpleNamespace(base_url=url)), url
    remote = SimpleNamespace(base_url="https://gateway.example.com")
    assert not preflight.is_local(remote)
