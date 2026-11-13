"""The alert feed: a finished run as an alerting system reads it.

An alerting consumer pages on what this file says, so the tests pin the readings that would
page wrongly if they slipped: a known failure called a regression, a run that tested nothing
read as a fleet verdict, a failure pinned on the layer that only passed it through.
"""

from __future__ import annotations

import json
import subprocess
import sys

from llmdv.cli import job_cmd
from llmdv.cli.compare_cmd import compare_run
from llmdv.cli.exit_codes import OK
from llmdv.cli.main import build_parser
from llmdv.compare.diff import compare_window
from llmdv.compare.report import COMPARISON_JSON
from llmdv.evidence import alerts
from llmdv.output import manifest
from tests.fakes import SRC, FakeFleet, profile_yaml


def rec(case, outcome, ingress="gateway", kind="litellm", variant="chat_completions", **extra):
    return {
        "case_id": case,
        "outcome": outcome,
        "ingress": ingress,
        "ingress_kind": kind,
        "variant": variant,
        "surface": variant,
        "profile_digest": "d1",
        **extra,
    }


def gateway(case, outcome, **extra):
    return rec(case, outcome, "gateway", "litellm", **extra)


def router(case, outcome, **extra):
    return rec(case, outcome, "router", "llmd_router", **extra)


def transitions(history, after):
    delta = compare_window(history, after)
    return {case: t for (_, case, _), t in delta.transitions.items()}


# ------------------------------------------------------------------- transitions from compare


def test_each_compare_verdict_has_one_word_for_a_machine_reader():
    before = [
        gateway("a", "pass"),
        gateway("b", "fail"),
        gateway("c", "fail"),
        gateway("d", "pass"),
    ]
    after = [
        gateway("a", "fail"),
        gateway("b", "fail"),
        gateway("c", "pass"),
        gateway("d", "pass"),
        gateway("e", "fail"),
    ]
    assert transitions([before], after) == {
        "a": "regressed",
        "b": "failing",
        "c": "recovered",
        "d": "passing",
        "e": "new",
    }


def test_a_window_that_disagrees_is_flaky_not_a_regression():
    history = [[gateway("a", "pass")], [gateway("a", "fail")], [gateway("a", "pass")]]
    assert transitions(history, [gateway("a", "fail")]) == {"a": "flaky"}


def test_a_case_skipped_now_has_no_transition():
    after = [gateway("a", "skip", skip_class="undeclared")]
    assert transitions([[gateway("a", "pass")]], after) == {}


def test_a_case_that_starts_running_and_fails_is_new_not_regressed():
    before = [gateway("a", "skip", skip_class="requires_load")]
    assert transitions([before], [gateway("a", "fail")]) == {"a": "new"}


# ------------------------------------------------------------------------ the feed from a run


def run_dir(tmp_path, records, verification_class="fleet", comparison=None, **outcome_extra):
    d = tmp_path / "2026-10-08T02-00-00Z-abcd"
    d.mkdir(parents=True)
    (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    body = {
        "schema": "llmdv.outcome/1",
        "run_id": d.name,
        "profile": {"name": "fleet-a"},
        "labels": {"trigger": "nightly"},
        "started": "2026-10-08T02:00:00+00:00",
        "finished": "2026-10-08T02:40:00+00:00",
        "verification": {"exit_code": 1, "class": verification_class, "counts": {}},
        **outcome_extra,
    }
    (d / "outcome.json").write_text(json.dumps(body))
    if comparison is not None:
        (d / COMPARISON_JSON).write_text(json.dumps(comparison))
    return d


def compared(history, after):
    delta = compare_window(history, after)
    from llmdv.compare.report import as_record

    return as_record(delta, "2026-10-07T02-00-00Z-0000", "2026-10-08T02-00-00Z-abcd")


def results_of(feed):
    return {(r["case_id"], r["ingress"]): r for r in feed["results"]}


def test_a_regression_is_marked_and_a_known_failure_is_not(tmp_path):
    before = [router("a", "pass"), router("b", "fail")]
    after = [router("a", "fail", reason="finish_reason abort\nmore"), router("b", "fail")]
    feed = alerts.build(run_dir(tmp_path, after, comparison=compared([before], after)))

    got = results_of(feed)
    assert got[("a", "router")]["transition"] == "regressed"
    assert got[("b", "router")]["transition"] == "failing"
    assert got[("a", "router")]["reason"] == "finish_reason abort"
    assert feed["comparison"]["made"] is True
    assert feed["comparison"]["baseline"] == "2026-10-07T02-00-00Z-0000"


def test_without_a_comparison_no_verdict_can_read_as_a_regression(tmp_path):
    feed = alerts.build(run_dir(tmp_path, [router("a", "fail"), router("b", "pass")]))

    assert {r["transition"] for r in feed["results"]} == {"unbaselined"}
    assert feed["comparison"] == {"made": False, "reason": "no comparison was requested"}


def test_a_comparison_that_could_not_be_made_says_why(tmp_path):
    comparison = {"schema": "llmdv.comparison/1", "made": False, "reason": "archive unreachable"}
    feed = alerts.build(run_dir(tmp_path, [router("a", "fail")], comparison=comparison))

    assert feed["comparison"] == {"made": False, "reason": "archive unreachable"}
    assert feed["results"][0]["transition"] == "unbaselined"


def test_failing_at_the_outer_layer_only_points_at_the_outer_layer(tmp_path):
    records = [gateway("a", "fail"), router("a", "pass")]
    got = results_of(alerts.build(run_dir(tmp_path, records)))[("a", "gateway")]

    assert got["points_at"] == "gateway"
    assert got["points_at_kind"] == "litellm"
    assert got["basis"] == "fails at gateway, passes at router"


def test_failing_at_every_layer_points_at_the_innermost(tmp_path):
    """The gateway result is real, but blaming the gateway for what the router does sends the
    page to the wrong team."""
    records = [gateway("a", "fail"), router("a", "error")]
    got = results_of(alerts.build(run_dir(tmp_path, records)))

    assert got[("a", "gateway")]["points_at"] == "router"
    assert got[("a", "gateway")]["points_at_kind"] == "llmd_router"
    assert "and at gateway" in got[("a", "gateway")]["basis"]
    assert got[("a", "router")]["points_at"] == "router"


def test_a_single_layer_points_only_at_itself(tmp_path):
    got = alerts.build(run_dir(tmp_path, [router("a", "fail")]))["results"][0]

    assert got["points_at"] == "router"
    assert got["basis"] == "fails at router, the innermost layer that ran this case"


def test_a_layer_that_skipped_the_case_isolates_nothing(tmp_path):
    records = [gateway("a", "fail"), router("a", "skip", skip_class="undeclared")]
    got = results_of(alerts.build(run_dir(tmp_path, records)))

    assert ("a", "router") not in got, "a skip is not an observation"
    assert got[("a", "gateway")]["points_at"] == "gateway"


def test_variants_of_a_case_are_attributed_separately(tmp_path):
    records = [
        gateway("a", "fail", variant="messages"),
        router("a", "pass", variant="messages"),
        gateway("a", "fail", variant="chat_completions"),
        router("a", "fail", variant="chat_completions"),
    ]
    feed = alerts.build(run_dir(tmp_path, records))
    points = {(r["variant"], r["ingress"]): r.get("points_at") for r in feed["results"]}

    assert points[("messages", "gateway")] == "gateway"
    assert points[("chat_completions", "gateway")] == "router"


def test_a_run_that_tested_nothing_does_not_observe_the_fleet(tmp_path):
    d = run_dir(
        tmp_path,
        [],
        verification_class="environment",
        preflight_problems=["router: connection refused\nmore detail"],
    )
    feed = alerts.build(d)

    assert feed["fleet_observed"] is False
    assert feed["verification"]["class"] == "environment"
    assert feed["preflight_problems"] == ["router: connection refused"]
    assert feed["results"] == []


def test_failures_come_first_so_a_cap_can_only_drop_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "MAX_RESULTS", 2)
    records = [router(f"p{i}", "pass") for i in range(3)] + [router("f", "fail")]
    feed = alerts.build(run_dir(tmp_path, records))

    assert feed["results"][0]["case_id"] == "f"
    assert len(feed["results"]) == 2 and feed["truncated"] == 2


def test_a_run_without_an_outcome_has_no_feed(tmp_path):
    assert alerts.build(tmp_path) is None


# --------------------------------------------------------------- compare writes its data copy


def archive(root, profile, runs):
    for run_id, records in runs.items():
        d = root / profile / run_id
        d.mkdir(parents=True)
        (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in records))
        # compare only takes a nightly run as its baseline.
        (d / "outcome.json").write_text(json.dumps({"labels": {"trigger": "nightly"}}))
        manifest.write(d)
    return f"file://{root}"


def test_compare_writes_its_verdicts_as_data_beside_the_report(tmp_path):
    uri = archive(tmp_path / "arch", "p", {"2026-01-01T00Z": [router("a", "pass")]})
    d = tmp_path / "run" / "2026-01-02T00Z"
    d.mkdir(parents=True)
    (d / "results.jsonl").write_text(json.dumps(router("a", "fail", run_id=d.name)) + "\n")

    compare_run(d, uri, "p")
    record = json.loads((d / COMPARISON_JSON).read_text())

    assert record["made"] is True and record["regressed"] is True
    assert record["baseline"] == "2026-01-01T00Z"
    assert record["transitions"] == [
        {
            "ingress": "router",
            "case_id": "a",
            "variant": "chat_completions",
            "transition": "regressed",
        }
    ]
    published = tmp_path / "arch" / "p" / "2026-01-02T00Z" / COMPARISON_JSON
    assert published.is_file(), "the data copy ships with the run, like the report"


def test_compare_records_why_it_made_no_comparison(tmp_path):
    uri = archive(tmp_path / "arch", "p", {})
    d = tmp_path / "run" / "2026-01-02T00Z"
    d.mkdir(parents=True)
    (d / "results.jsonl").write_text(json.dumps(router("a", "fail")) + "\n")

    compare_run(d, uri, "p")
    record = json.loads((d / COMPARISON_JSON).read_text())

    assert record["made"] is False
    assert "no earlier nightly run" in record["reason"]


# ------------------------------------------------------------------- llmdv job, end to end


def job(tmp_path, base_url, model, archive_dir):
    tmp_path.mkdir(parents=True, exist_ok=True)
    profile = tmp_path / "fake.yaml"
    profile.write_text(profile_yaml(base_url, model))
    command = [
        sys.executable, "-m", "llmdv.cli.main", "job",
        "--profile", str(profile), "--work-dir", str(tmp_path / "work"),
        "--remote-output", f"file://{archive_dir}", "--pod-logs", "off",
        "--compare", "-k", "test_chat_basic and not sdk",
    ]  # fmt: skip
    env = {"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"}
    return subprocess.run(  # noqa: S603 - fixed interpreter and args
        command, capture_output=True, text=True, cwd=SRC.parent, env=env
    )


def last_line(result):
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_job_log_ends_with_the_feed_and_a_regression_is_named(tmp_path):
    """Night one passes; night two the fleet aborts on a 200. The feed of night two must say
    `regressed`, carry the job's own exit code, and be the last line of the log."""
    archive_dir = tmp_path / "archive"
    with FakeFleet() as fleet:
        first = job(tmp_path / "one", fleet.base_url, fleet.model, archive_dir)
    with FakeFleet(abort_finish_reason=True) as fleet:
        second = job(tmp_path / "two", fleet.base_url, fleet.model, archive_dir)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 1, second.stderr
    night_one, night_two = last_line(first), last_line(second)
    assert night_one["schema"] == alerts.SCHEMA
    assert night_one["comparison"]["made"] is False
    assert night_one["job_exit_code"] == 0
    assert night_two["fleet_observed"] is True
    assert night_two["job_exit_code"] == 1
    assert night_two["comparison"]["baseline"] == night_one["run_id"]
    failing = [r for r in night_two["results"] if r["outcome"] in ("fail", "error")]
    assert failing and {r["transition"] for r in failing} == {"regressed"}
    assert all(r["evidence"] for r in failing)
    assert night_two["publication"]["status"] == "published"


def test_a_run_that_could_not_reach_the_fleet_says_so_in_the_feed(tmp_path):
    with FakeFleet() as fleet:
        base_url, model = fleet.base_url, fleet.model
    # The fleet is gone: preflight cannot reach it, which is exit 3, not a fleet verdict.
    result = job(tmp_path, base_url, model, tmp_path / "archive")

    assert result.returncode == 3, result.stderr
    feed = last_line(result)
    assert feed["fleet_observed"] is False
    assert feed["verification"]["class"] == "environment"
    assert feed["preflight_problems"]
    assert feed["job_exit_code"] == 3


# ------------------------------------------------------------------- the feed is advisory


def job_in_process(tmp_path, monkeypatch):
    """`llmdv job` with verify, publication, notification and the log replaced by recorders."""
    calls, lines = [], []

    def verify(args, run, passthrough):
        made = run_dir(tmp_path / "made", [router("a", "pass")], verification_class="ok")
        run.parent.mkdir(parents=True)
        made.rename(run)
        return OK

    monkeypatch.setattr(job_cmd, "_verify", verify)
    monkeypatch.setattr(job_cmd, "publish_run", lambda *a, **k: calls.append("publish") or OK)
    monkeypatch.setattr(job_cmd, "notify_run", lambda *a: calls.append("notify") or OK)
    monkeypatch.setattr(job_cmd, "emit", lines.append)
    monkeypatch.setenv(job_cmd.WEBHOOK_ENV, "http://127.0.0.1:9/hook")
    args, unknown = build_parser().parse_known_args(
        ["job", "--profile", "fake.yaml", "--work-dir", str(tmp_path / "work"),
         "--remote-output", f"file://{tmp_path / 'archive'}", "--pod-logs", "off"]
    )  # fmt: skip
    args.pytest_args = unknown
    return job_cmd.cmd_job(args), calls, lines


def test_a_feed_that_cannot_be_built_costs_neither_delivery_nor_the_exit_code(
    tmp_path, monkeypatch
):
    def broken(run):
        raise ValueError("unreadable results")

    monkeypatch.setattr(alerts, "build", broken)
    code, calls, lines = job_in_process(tmp_path, monkeypatch)

    assert code == OK
    assert calls == ["publish", "notify"]
    assert not any(line.startswith('{"schema":"llmdv.alerts/1"') for line in lines)


def test_a_feed_that_cannot_be_written_is_still_the_last_line_of_the_log(tmp_path, monkeypatch):
    def broken(run, feed):
        raise OSError("read-only file system")

    monkeypatch.setattr(alerts, "write", broken)
    code, calls, lines = job_in_process(tmp_path, monkeypatch)

    assert code == OK
    assert calls == ["publish", "notify"]
    feed = json.loads(lines[-1])
    assert feed["schema"] == alerts.SCHEMA and feed["job_exit_code"] == OK
