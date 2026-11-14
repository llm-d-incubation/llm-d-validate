"""The stack a run observed is recorded with it, and a comparison says when it changed."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from llmdv.cli.main import main
from llmdv.compare.archive import read_stack
from llmdv.compare.diff import attach_stack, compare, mixed_builds, stack_changes
from llmdv.compare.report import as_record, render
from llmdv.evidence.stack import ingress_kinds, observed_stack, stack_from_run
from llmdv.output import manifest
from tests.fakes import FakeFleet, profile_yaml

KINDS = {"litellm": "litellm", "epp": "llmd_router", "engine": "vllm"}
OLD = "vllm-0.31.0+g5b2a143-tp8-dp4-ep-926de753"
NEW = "vllm-0.31.0+g2107750-tp8-dp4-ep-926de753"


def exchange(ingress="litellm", fingerprint=OLD, headers=None):
    return {
        "ingress": ingress,
        "response_headers": {
            "server": "uvicorn",
            "x-litellm-version": "1.97.0",
            "x-litellm-model-api-base": "http://internal-svc.ns.svc.cluster.local:8000",
            "x-envoy-decorator-operation": "svc-name.ns.svc.cluster.local:80/*",
            "x-llm-d-inference-objective": "default",
            **(headers or {}),
        },
        "response_body": {"id": "chatcmpl-1", "system_fingerprint": fingerprint},
    }


def test_each_layer_records_the_signals_its_adapter_declares_and_internal_names_stay_out():
    exchanges = [
        exchange(),
        exchange(),
        exchange(fingerprint=NEW),
        exchange("epp"),
        exchange("engine"),
    ]
    stack = observed_stack(exchanges, KINDS)
    litellm = stack["litellm"]
    assert litellm["system_fingerprint"] == {OLD: 2, NEW: 1}
    assert litellm["x-litellm-version"] == {"1.97.0": 3}
    assert litellm["server"] == {"uvicorn": 3}
    # The gateway adapter does not claim the router's headers; the router adapter does.
    assert "x-llm-d-inference-objective" not in litellm
    assert stack["epp"]["x-llm-d-inference-objective"] == {"default": 1}
    assert stack["epp"]["system_fingerprint"] == {OLD: 1}
    # The engine knows only the base signals.
    assert set(stack["engine"]) == {"server", "system_fingerprint"}
    for layer in stack.values():
        assert (
            "x-litellm-model-api-base" not in layer and "x-envoy-decorator-operation" not in layer
        )


def test_an_ingress_of_unknown_kind_gets_the_base_signals():
    stack = observed_stack([exchange()], {"litellm": "not-a-kind"})
    assert set(stack["litellm"]) == {"server", "system_fingerprint"}
    assert set(observed_stack([exchange()])["litellm"]) == {"server", "system_fingerprint"}


def test_ingress_kinds_come_from_the_results():
    records = [
        {"ingress": "litellm", "ingress_kind": "litellm"},
        {"ingress": "epp", "ingress_kind": "llmd_router"},
    ]
    assert ingress_kinds(records) == {"litellm": "litellm", "epp": "llmd_router"}


def test_a_run_directory_is_read_through_a_torn_last_line(tmp_path):
    lines = [json.dumps(exchange()), json.dumps(exchange(fingerprint=NEW)), '{"ingress": "lit']
    (tmp_path / "exchanges.jsonl").write_text("\n".join(lines))
    (tmp_path / "results.jsonl").write_text(
        json.dumps({"ingress": "litellm", "ingress_kind": "litellm"})
    )
    stack = stack_from_run(tmp_path)
    assert stack["litellm"]["system_fingerprint"] == {OLD: 1, NEW: 1}
    assert stack["litellm"]["x-litellm-version"] == {"1.97.0": 2}
    assert stack_from_run(tmp_path / "missing") == {}


def test_two_builds_in_one_run_is_a_rollout():
    stack = observed_stack([exchange(), exchange(fingerprint=NEW)])
    assert mixed_builds(stack) == [f"litellm: system_fingerprint {OLD} ×1, {NEW} ×1"]
    assert mixed_builds(observed_stack([exchange(), exchange()])) == []


def test_a_changed_engine_is_one_line_and_an_unchanged_one_is_none():
    before = observed_stack([exchange()], KINDS)
    after = observed_stack(
        [exchange(fingerprint=NEW, headers={"x-litellm-version": "1.104.1"})], KINDS
    )
    assert stack_changes(before, after) == [
        f"litellm: system_fingerprint {OLD} → {NEW}",
        "litellm: x-litellm-version 1.97.0 → 1.104.1",
    ]
    assert stack_changes(before, observed_stack([exchange()], KINDS)) == []


def test_a_signal_that_appears_or_disappears_is_named():
    before = observed_stack([exchange()])
    after = observed_stack([{"ingress": "litellm", "response_headers": {"server": "uvicorn"}}])
    assert "litellm: system_fingerprint " + OLD + " → absent" in stack_changes(before, after)


def test_the_comparison_says_the_stack_changed_before_the_delta():
    before = observed_stack([exchange()])
    after = observed_stack([exchange(fingerprint=NEW), exchange()])
    delta = attach_stack(
        compare(
            [{"case_id": "a", "outcome": "pass", "ingress": "litellm"}],
            [{"case_id": "a", "outcome": "fail", "ingress": "litellm"}],
        ),
        before,
        after,
    )
    text = render(delta, "p", "r1", "r2")
    assert "*The stack changed between these runs.*" in text
    assert text.index("stack changed") < text.index("Newly failing")
    assert "straddled a rollout" in text
    record = as_record(delta, "r1", "r2")
    assert record["stack"]["changes"] == [f"litellm: system_fingerprint {OLD} → {NEW}, {OLD}"]
    assert record["stack"]["mixed"] and record["stack"]["after"] == after


def test_runs_that_predate_the_block_compare_without_a_stack_line():
    delta = attach_stack(compare([], []), {}, {})
    assert delta.stack_changes == [] and delta.mixed_stack == []
    assert "stack changed" not in render(delta, "p", "r1", "r2")


def _archive(tmp_path, runs: dict[str, dict]):
    for run_id, stack in runs.items():
        d = tmp_path / "p" / run_id
        d.mkdir(parents=True)
        (d / "results.jsonl").write_text(
            json.dumps({"case_id": "a", "outcome": "pass", "ingress": "litellm"})
        )
        (d / "outcome.json").write_text(
            json.dumps({"labels": {"trigger": "nightly"}, "stack": stack})
        )
        manifest.write(d)
    return f"file://{tmp_path}"


def test_a_published_stack_is_read_back_and_compared(tmp_path, capsys):
    before, after = observed_stack([exchange()]), observed_stack([exchange(fingerprint=NEW)])
    uri = _archive(tmp_path, {"2026-01-01T00Z": before, "2026-01-02T00Z": after})
    assert read_stack(uri, "p", "2026-01-01T00Z") == before
    assert read_stack(uri, "p", "2026-01-03T00Z") == {}
    assert main(["compare", "--from", uri, "--profile", "p"]) == 0
    assert f"system_fingerprint {OLD} → {NEW}" in capsys.readouterr().out


def test_a_router_prefix_never_captures_a_backend_endpoint():
    """`x-llm-d-destination-endpoint` is a backend header (an IP and port) that happens to share
    the router's prefix; it must stay out of the published outcome."""
    ex = exchange("epp", headers={"x-llm-d-destination-endpoint": "10.0.0.7:8000"})
    epp = observed_stack([ex], KINDS)["epp"]
    assert "x-llm-d-destination-endpoint" not in epp
    assert epp["x-llm-d-inference-objective"] == {"default": 1}


SRC = Path(__file__).resolve().parents[1] / "src"


def test_a_finished_run_records_the_stack_it_saw(tmp_path):
    """The one place the block is written is _finish; a unit test of stack_from_run cannot
    tell whether verify still calls it."""
    with FakeFleet() as fleet:
        profile = tmp_path / "fake.yaml"
        profile.write_text(profile_yaml(fleet.base_url, fleet.model))
        run_dir = tmp_path / "run"
        result = subprocess.run(  # noqa: S603 - fixed interpreter and args
            [sys.executable, "-m", "llmdv.cli.main", "verify", "--profile", str(profile),
             "--local-output", str(run_dir), "--pod-logs", "off", "-k", "models_lists"],
            cwd=SRC.parent, env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True,
        )  # fmt: skip
    assert result.returncode == 0, result.stderr[-2000:]
    outcome = json.loads((run_dir / "outcome.json").read_text())
    stack = outcome["stack"]
    assert stack, outcome
    ((ingress, signals),) = stack.items()
    assert "server" in signals and sum(signals["server"].values()) >= 1, signals
