"""Reading `llmdv.results/1` records with and without its optional fields, and refusing others.

Archives keep old runs, so a reader meets records written before and after an optional field
existed in one window. A check's `observed` may be any JSON value, a check may add `rule` and
`samples`, and a record may add `claims`; none changes what a reader keys on. These tests write
one run both ways and require every reader -- run summary, failure groups, dashboard, alert
feed, comparison, the stack reading, the known-failure counts and the CI job summary -- to reach
the same answer from each. A record under
an unknown major is refused, with the file and the version named.
"""

from __future__ import annotations

import base64
import copy
import gzip
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from llmdv.cli import publish_cmd, verify_cmd
from llmdv.cli.exit_codes import USAGE
from llmdv.cli.main import main
from llmdv.compare import archive
from llmdv.compare.diff import compare_window
from llmdv.compare.report import COMPARISON_JSON, as_record
from llmdv.evidence import alerts, dashboard, outcome
from llmdv.evidence.stack import ingress_kinds
from llmdv.output import manifest
from llmdv.profile.known import first_failed_check
from llmdv.results import read as results_file
from llmdv.results.read import UnreadableSchema
from llmdv.results.record import ResultRecord, ResultsWriter

REPO = Path(__file__).resolve().parents[1]

PROFILE = """\
schema: llmdv.profile/1
name: p
model: {public_id: m}
ingresses:
  - id: gateway
    kind: litellm
    base_url: https://gateway.example
    surfaces: [chat_completions]
  - id: router
    kind: llmd_router
    base_url: https://router.example
    surfaces: [chat_completions]
"""

# A structured `observed`, and the text an earlier writer made of the same value.
OBSERVED = {"mode_count": 4, "distinct": 2}
RULE = {"kind": "count", "n": 5, "target": "mode", "min": 5, "max": 5}
SAMPLES = [
    {"exchange": n, "value": f"sha256:{digest}", "preview": "Purple owls whisper"}
    for n, digest in enumerate(["9b1e04c2d7aa"] * 4 + ["0c4f19ab33e1"], start=1)
]


def record(case, outcome, ingress="gateway", checks=(), **extra):
    kind = {"gateway": "litellm", "router": "llmd_router"}[ingress]
    return {
        "schema": "llmdv.results/1", "run_id": "r", "profile": "p", "profile_digest": "d1",
        "nodeid": f"src/llmdv/suites/x/test_x.py::test_{case}[{ingress}]", "case_id": f"x.{case}",
        "variant": None, "ingress": ingress, "ingress_kind": kind, "surface": "chat_completions",
        "outcome": outcome, "skip_class": None, "reason": None, "duration_s": 0.1,
        "http_status": None, "served_by": {}, "checks": list(checks), "evidence": None,
        "known_failure": None, "ts": "2026-11-01T00:00:00+00:00",
        **extra,
    }  # fmt: skip


def v1_records(run_id="r"):
    """One run as `/1` records it: two layers, every outcome, a known failure."""
    repeat = {
        "name": "repeat_text", "observed": str(OBSERVED),
        "expected": "at least 5 of 5 samples equal the most common", "ok": False,
    }  # fmt: skip
    envelope = {"name": "error_envelope", "observed": None, "expected": "an envelope", "ok": False}
    status = {"name": "http_status", "observed": 200, "expected": "200", "ok": True}
    rows = [
        record("ok", "pass", checks=[status]),
        record("ok", "pass", "router", checks=[status]),
        record("repeat", "fail", checks=[status, repeat], reason="AssertionError: repeat_text"),
        record("repeat", "pass", "router", checks=[status]),
        record(
            "envelope",
            "fail",
            checks=[envelope],
            reason="AssertionError: error_envelope",
            known_failure={"status": "known", "issue": "example#1", "check": "error_envelope"},
        ),  # fmt: skip
        record("absent", "skip", skip_class="undeclared", reason="Skipped: [undeclared] no"),
        record("broken", "error", "router", reason="ConnectionError: reset"),
    ]
    for row in rows:
        row["run_id"] = run_id
    return rows


def extended_records(run_id="r"):
    """The same run with the optional fields: a structured `observed`, the rule and samples behind
    the statistical check, and the claims each record serves."""
    rows = copy.deepcopy(v1_records(run_id))
    for row in rows:
        row["claims"] = ["prefix-cache-affinity"] if row["case_id"] == "x.repeat" else []
        for check in row["checks"]:
            if check["name"] == "repeat_text":
                check.update(observed=dict(OBSERVED), rule=dict(RULE), samples=list(SAMPLES))
    return rows


def write_jsonl(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def run_dir(tmp_path: Path, name: str, rows) -> Path:
    """A finished run directory, with the comparison against a clean baseline, as `llmdv job`
    leaves it."""
    run = tmp_path / name
    run.mkdir()
    write_jsonl(run / "results.jsonl", rows)
    (run / "profile.used.yaml").write_text(PROFILE)
    body = {
        "schema": "llmdv.outcome/1", "run_id": "r", "profile": {"name": "p"},
        "started": "2026-11-01T00:00:00+00:00", "finished": "2026-11-01T00:01:00+00:00",
        "verification": {"exit_code": 1, "class": "fleet", "counts": {}},
    }  # fmt: skip
    (run / "outcome.json").write_text(json.dumps(body))
    (run / "env.json").write_text(json.dumps({"run_id": "r"}))
    baseline = [record(case, "pass", ingress) for case, ingress in _keys(rows)]
    comparison = as_record(compare_window([baseline], rows), "b", "r")
    (run / COMPARISON_JSON).write_text(json.dumps(comparison))
    return run


def _keys(rows):
    return [(r["case_id"].split(".", 1)[1], r["ingress"]) for r in rows]


@pytest.fixture
def runs(tmp_path):
    return run_dir(tmp_path, "v1", v1_records()), run_dir(tmp_path, "full", extended_records())


def without_checks(test: dict) -> dict:
    return {k: v for k, v in test.items() if k not in ("checks", "rerun")}


# ------------------------------------------------------------------------- the same answers


def test_records_with_and_without_optional_fields_read_back_alike(runs):
    v1, full = runs
    assert len(results_file.read(v1 / "results.jsonl")) == 7
    assert [r["case_id"] for r in outcome.read_results(full)] == [
        r["case_id"] for r in outcome.read_results(v1)
    ]
    assert verify_cmd._read_records(full) == extended_records()


def test_the_run_summary_is_the_same(runs):
    v1, full = (outcome.read_results(run) for run in runs)
    assert outcome.summarise(full) == outcome.summarise(v1)
    assert outcome.known_summary(full) == outcome.known_summary(v1)
    assert [first_failed_check(r["checks"]) for r in full] == [
        first_failed_check(r["checks"]) for r in v1
    ]


def test_failure_groups_and_dashboard_data_are_the_same(runs):
    v1, full = runs
    assert dashboard.summary(full) == dashboard.summary(v1)
    one, two = dashboard.build(v1, 2000, 10), dashboard.build(full, 2000, 10)
    for field in ("counts", "skips", "by_suite", "clusters", "slowest", "exit"):
        assert two[field] == one[field], field
    keys = ["repeat_text", "error_envelope", "ConnectionError: reset"]
    assert [g["key"] for g in two["clusters"]] == keys
    assert [without_checks(t) for t in two["tests"]] == [without_checks(t) for t in one["tests"]]


def test_a_structured_observed_reaches_the_dashboard_with_its_rule_and_samples(runs):
    """The page shows a structured value as JSON, as it already does for a passing check."""
    v1, full = runs
    one, two = (
        next(t for t in dashboard.build(run, 2000, 10)["tests"] if t["case_id"] == "x.repeat")
        for run in (v1, full)
    )
    assert [c["name"] for c in two["checks"]] == [c["name"] for c in one["checks"]]
    repeat = two["checks"][1]
    assert repeat["observed"] == OBSERVED
    assert str(repeat["observed"]) == one["checks"][1]["observed"]
    assert repeat["rule"] == RULE and len(repeat["samples"]) == RULE["n"]
    page = dashboard.render(dashboard.build(full, 2000, 10))
    assert "typeof k.observed === 'string'" in page


def test_alert_attribution_is_the_same(runs):
    v1, full = (alerts.build(run) for run in runs)
    assert full == v1
    failing = {(r["case_id"], r["ingress"]): r for r in full["results"] if r["outcome"] == "fail"}
    assert failing[("x.repeat", "gateway")]["points_at"] == "gateway"
    assert failing[("x.envelope", "gateway")]["known_failure"]


def test_the_comparison_is_the_same_and_a_window_may_mix_both(tmp_path):
    root = tmp_path / "archive"
    baseline = [record(case, "pass", ingress) for case, ingress in _keys(v1_records())]
    for run_id, rows in {
        "2026-11-01T00Z": baseline,
        "2026-11-02T00Z": v1_records("2026-11-02T00Z"),
        "2026-11-03T00Z": extended_records("2026-11-03T00Z"),
    }.items():
        d = root / "p" / run_id
        d.mkdir(parents=True)
        write_jsonl(d / "results.jsonl", rows)
        manifest.write(d)
    uri = f"file://{root}"

    def read(run_id):
        return archive.read_results(uri, "p", run_id)

    against_v1 = compare_window([read("2026-11-01T00Z")], read("2026-11-02T00Z"))
    against_full = compare_window([read("2026-11-01T00Z")], read("2026-11-03T00Z"))
    assert as_record(against_full, "b", "a") == as_record(against_v1, "b", "a")
    assert against_full.regressed and against_full.newly_failing == against_v1.newly_failing
    # A run with the optional fields and one without, of the same verdicts: nothing changed.
    mixed = compare_window([read("2026-11-02T00Z")], read("2026-11-03T00Z"))
    assert not mixed.regressed and not mixed.newly_failing
    assert main(["compare", "--from", uri, "--profile", "p", "--no-fail"]) == 0


def test_the_stack_reading_is_the_same(runs):
    v1, full = (outcome.read_results(run) for run in runs)
    expected = {"gateway": "litellm", "router": "llmd_router"}
    assert ingress_kinds(full) == ingress_kinds(v1) == expected


def live_summary():
    path = REPO / ".github" / "scripts" / "live_summary.py"
    spec = importlib.util.spec_from_file_location("live_summary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def summarize(monkeypatch, capsys, run: Path) -> str:
    monkeypatch.setattr(sys, "argv", ["live_summary.py", str(run), "--profile", "p"])
    assert live_summary().main() == 0
    return capsys.readouterr().out


def test_the_ci_job_summary_is_the_same(runs, monkeypatch, capsys):
    v1, full = runs
    one, two = summarize(monkeypatch, capsys, v1), summarize(monkeypatch, capsys, full)
    assert two == one
    assert "`repeat_text`" in two


# ---------------------------------------------------------------------------- refusing others


def unknown(rows):
    """The same records with the third one written under a major no reader knows."""
    rows = copy.deepcopy(rows)
    rows[2]["schema"] = "llmdv.results/3"
    return rows


def test_a_record_without_a_schema_reads_as_the_first_version(tmp_path):
    rows = v1_records()
    for row in rows:
        del row["schema"]
    write_jsonl(tmp_path / "results.jsonl", rows)
    assert len(results_file.read(tmp_path / "results.jsonl")) == 7


@pytest.mark.parametrize(
    "schema", ["llmdv.results/2", "llmdv.results/3", "llmdv.results/0", "llmdv.outcome/1", 2]
)
def test_an_unknown_schema_is_refused_naming_the_file_line_and_version(tmp_path, schema):
    rows = extended_records()
    rows[2]["schema"] = schema
    path = tmp_path / "results.jsonl"
    write_jsonl(path, rows)
    with pytest.raises(UnreadableSchema) as raised:
        results_file.read(path)
    message = str(raised.value)
    assert str(path) in message and "line 3" in message and repr(schema) in message


def test_every_local_reader_refuses_an_unknown_major(tmp_path):
    run = run_dir(tmp_path, "v3", unknown(extended_records()))
    for reader in (outcome.read_results, verify_cmd._read_records, alerts.build, dashboard.summary):
        with pytest.raises(UnreadableSchema, match=r"results\.jsonl, line 3: .*llmdv\.results/3"):
            reader(run)
    assert dashboard.write_best_effort(run) is None


def test_compare_refuses_an_unknown_major_naming_the_run_file(tmp_path):
    root = tmp_path / "archive"
    runs = {"2026-11-01T00Z": v1_records(), "2026-11-02T00Z": unknown(v1_records())}
    for run_id, rows in runs.items():
        d = root / "p" / run_id
        d.mkdir(parents=True)
        write_jsonl(d / "results.jsonl", rows)
        manifest.write(d)
    uri = f"file://{root}"
    with pytest.raises(archive.InvalidRun) as raised:
        archive.read_results(uri, "p", "2026-11-02T00Z")
    assert "p/2026-11-02T00Z/results.jsonl, line 3" in str(raised.value)
    assert "'llmdv.results/3'" in str(raised.value)
    with pytest.raises(archive.InvalidRun, match="llmdv.results/3"):
        archive.read_local_results(root / "p" / "2026-11-02T00Z")
    assert main(["compare", "--from", uri, "--profile", "p"]) == USAGE


def test_analyze_and_publish_refuse_an_unknown_major(tmp_path):
    run = run_dir(tmp_path, "v3", unknown(v1_records()))
    assert main(["analyze", str(run)]) == USAGE
    assert not (run / "dashboard.html").exists()
    (run / "outcome.json").unlink()  # a crashed run: publish rebuilds its outcome from results
    assert publish_cmd.publish_run(run, f"file://{tmp_path / 'store'}", 137) == USAGE
    assert not (tmp_path / "store").exists()


def test_the_ci_job_summary_names_an_unknown_major_instead_of_counting_it(
    tmp_path, monkeypatch, capsys
):
    out = summarize(monkeypatch, capsys, run_dir(tmp_path, "v3", unknown(extended_records())))
    assert "Results not summarized: results.jsonl, line 3: schema 'llmdv.results/3'" in out
    assert "### By suite" not in out


def _strict(text: str):
    """Parse as a browser's JSON.parse does: NaN and Infinity are not JSON."""

    def refuse(name):
        raise ValueError(f"not strict JSON: {name}")

    return json.loads(text, parse_constant=refuse)


def test_a_non_finite_number_is_written_to_results_as_strict_json(tmp_path):
    check = {
        "name": "effort_order",
        "observed": {"medians": {"low": float("nan")}, "ratio": float("-inf")},
        "ok": False,
        "rule": {"kind": "order", "n": 1, "slack": float("nan")},
        "samples": [{"exchange": 0, "value": float("inf"), "preview": "inf"}, {"value": 1.5}],
    }
    writer = ResultsWriter(tmp_path / "results.jsonl")
    writer.append(ResultRecord(run_id="r", nodeid="n", outcome="fail", profile="p", checks=[check]))
    writer.close()
    (row,) = [_strict(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    (written,) = row["checks"]
    assert written["rule"]["slack"] == "NaN"
    assert written["observed"] == {"medians": {"low": "NaN"}, "ratio": "-Infinity"}
    assert [s["value"] for s in written["samples"]] == ["Infinity", 1.5]


def test_a_non_finite_number_in_an_older_run_reaches_the_dashboard_as_strict_json(tmp_path):
    """A run written before the writer refused them still renders a page a browser can parse."""
    rows = extended_records()
    repeat = next(c for row in rows for c in row["checks"] if c["name"] == "repeat_text")
    repeat["rule"] = {"kind": "order", "n": 3, "slack": float("nan")}
    repeat["samples"][0]["value"] = float("inf")
    run = run_dir(tmp_path, "nan", rows)
    assert "NaN" in (run / "results.jsonl").read_text()
    page = dashboard.write(run).read_text()
    packed = re.search(r"[A-Za-z0-9+/=]{500,}", page).group(0)
    data = _strict(gzip.decompress(base64.b64decode(packed)).decode())
    (shown,) = [c for t in data["tests"] for c in t.get("checks", []) if c.get("rule")]
    assert shown["rule"]["slack"] == "NaN" and shown["samples"][0]["value"] == "Infinity"
