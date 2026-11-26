"""Only the llmdv scheduler may run live cases concurrently, even with xdist installed."""

from __future__ import annotations

import argparse
import importlib.util
from types import SimpleNamespace

import pytest

from llmdv.cli import verify_cmd
from llmdv.cli.main import build_parser
from llmdv.evidence.outcome import Outcome
from tests.fakes import FakeFleet, profile_yaml, run_llmdv

HAS_XDIST = importlib.util.find_spec("xdist") is not None


def test_non_posix_concurrency_is_a_usage_error_before_planning(tmp_path, monkeypatch, caplog):
    args = build_parser().parse_args(["verify", "--profile", "unused.yaml", "--concurrency", "2"])
    monkeypatch.setattr(verify_cmd, "os", SimpleNamespace(name="nt", environ={}))
    monkeypatch.setattr(verify_cmd.log, "handlers", [caplog.handler])
    assert verify_cmd._verify(args, verify_cmd._Run(tmp_path, Outcome("run"))) == 2
    assert "requires POSIX" in caplog.text and "--concurrency 1" in caplog.text


@pytest.mark.parametrize(
    "extra",
    [
        ["-n", "4"],
        ["-n4"],
        ["-nauto"],
        ["--numprocesses", "2"],
        ["--numprocesses=auto"],
        ["--dist=loadscope"],
        ["--maxprocesses", "2"],
        ["--tx", "popen"],
        ["-d"],
        ["--px", "id=local"],
    ],
)
def test_parallel_arguments_are_recognised(extra):
    assert verify_cmd._parallel_args(["-x", *extra, "--tb=short"]) == [extra[0]]


@pytest.mark.parametrize("extra", [["-x"], ["--tb=short"], ["--no-header"], ["-vv"], []])
def test_ordinary_pytest_arguments_pass(extra):
    assert verify_cmd._parallel_args(extra) == []


def test_the_suite_session_always_blocks_xdist(tmp_path):
    args = argparse.Namespace(
        profile="p.yaml", evidence="failures", transcript_cap=None, ingress=[], client=[],
        surface=[], kubeconfig=None, context=None, kube_cli=None, keyword=None, marker=None,
        pytest_args=[],
    )  # fmt: skip
    argv = verify_cmd._build_pytest_argv(args, tmp_path, "run")
    assert ["-p", "no:xdist"] == argv[argv.index("no:xdist") - 1 : argv.index("no:xdist") + 1]


def _verify(tmp_path, fleet, *extra, env=None):
    profile = tmp_path / "fake.yaml"
    profile.write_text(profile_yaml(fleet.base_url, fleet.model))
    return run_llmdv(
        "verify", "--profile", str(profile), "--local-output", str(tmp_path / "run"),
        "--pod-logs", "off", "-k", "discovery", *extra,
        env={"TMPDIR": str(tmp_path), **(env or {})},
    )  # fmt: skip


def test_verify_refuses_parallel_workers_before_sending_anything(tmp_path):
    with FakeFleet() as fleet:
        result = _verify(tmp_path, fleet, "-n", "2")
        sent = len(fleet.seen)
    assert result.returncode == 2, result.stderr
    assert "one case at a time" in result.stderr
    assert sent == 0


@pytest.mark.skipif(not HAS_XDIST, reason="pytest-xdist is not installed (the `dev` extra)")
def test_verify_runs_serially_where_xdist_is_installed(tmp_path):
    with FakeFleet() as fleet:
        result = _verify(tmp_path, fleet)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not HAS_XDIST, reason="pytest-xdist is not installed (the `dev` extra)")
def test_workers_smuggled_in_through_pytest_addopts_are_a_usage_error(tmp_path):
    with FakeFleet() as fleet:
        result = _verify(tmp_path, fleet, env={"PYTEST_ADDOPTS": "-n 2"})
    assert result.returncode == 2, result.stderr
