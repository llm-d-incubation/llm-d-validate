"""A local tunnel that dies mid-run is the environment's fault, not the fleet's.

Preflight already turns a dead port-forward into exit 3 before the first case. These cover the
same tunnel dying after preflight passed: the case that found it gone is an error naming the
lost connection, later cases on that ingress are skipped, and the run exits 3 unless a real
failure also happened. Nothing is retried and no tunnel is restarted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from llmdv import plugin, preflight
from llmdv.http.client import Exchange
from llmdv.profile import loader
from tests.fakes import SRC, FakeFleet, profile_yaml, run_llmdv

SELECTION = ("-m", "openai_chat")


def _profile(tmp_path: Path, fleet: FakeFleet, timeouts: str | None = None) -> Path:
    text = profile_yaml(fleet.base_url, fleet.model)
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


def _state(ingress, exchange: Exchange) -> tuple[SimpleNamespace, dict]:
    client = SimpleNamespace(exchanges=[exchange], last=exchange)
    config = SimpleNamespace(_llmdv_connection_lost={})
    return config, {"clients": [client], "client_ingresses": {id(client): ingress}}


def test_a_connection_error_on_an_ingress_that_still_answers_its_health_path_stands(tmp_path):
    exchange = Exchange(
        "POST", "/v1/chat/completions", transport_error="ReadError: reset", connection_failed=True
    )
    with FakeFleet() as fleet:
        ingress = loader.load(_profile(tmp_path, fleet)).ingress("fake")
        config, state = _state(ingress, exchange)
        assert plugin._connection_lost(config, state, None, ingress) is None


def test_a_non_local_ingress_is_never_probed(tmp_path, monkeypatch):
    exchange = Exchange(
        "POST", "/v1/chat/completions", transport_error="ConnectError: refused",
        connection_failed=True,
    )  # fmt: skip
    probed = []
    monkeypatch.setattr(preflight, "is_local", lambda ingress: False)
    monkeypatch.setattr(preflight, "reachable", lambda ingress: probed.append(ingress) or (0, 0))
    with FakeFleet() as fleet:
        ingress = loader.load(_profile(tmp_path, fleet)).ingress("fake")
    config, state = _state(ingress, exchange)
    assert plugin._connection_lost(config, state, None, ingress) is None
    assert probed == []


def test_the_local_predicate_is_the_loopback_one_preflight_uses(tmp_path):
    with FakeFleet() as fleet:
        ingress = loader.load(_profile(tmp_path, fleet)).ingress("fake")
    assert preflight.is_local(ingress)
    remote = SimpleNamespace(base_url="https://gateway.example.com")
    assert not preflight.is_local(remote)
