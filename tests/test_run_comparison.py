"""Comparing two runs of the same profile.

The hard part is not the diff, it is deciding what a transition *means*. Three of these tests
exist because the obvious reading is wrong: a skip is not a failure, a case that starts running
and fails is not a regression, and the same case at two layers is two different questions.
"""

from llmdv.compare.diff import compare, digests, key, transport_failure
from llmdv.compare.report import as_record, render


def rec(case, outcome, ingress="litellm", variant="", skip_class=None, digest="d1"):
    r = {"case_id": case, "outcome": outcome, "ingress": ingress, "profile_digest": digest}
    if variant:
        r["variant"] = variant
    if skip_class:
        r["skip_class"] = skip_class
    return r


# ----------------------------------------------------------------- the four plain transitions


def test_a_case_that_passed_and_now_fails_is_the_regression():
    d = compare([rec("a", "pass")], [rec("a", "fail")])
    assert d.newly_failing == ["a@litellm"]
    assert d.regressed


def test_a_case_that_failed_and_now_passes_is_reported_not_silently_dropped():
    """Silence after a fix is how people stop trusting the report."""
    d = compare([rec("a", "fail")], [rec("a", "pass")])
    assert d.newly_passing == ["a@litellm"] and not d.regressed


def test_a_case_failing_in_both_runs_is_not_new():
    d = compare([rec("a", "fail")], [rec("a", "fail")])
    assert d.still_failing == ["a@litellm"] and d.newly_failing == []


def test_an_error_counts_as_failing_in_both_directions():
    assert compare([rec("a", "pass")], [rec("a", "error")]).newly_failing == ["a@litellm"]
    assert compare([rec("a", "error")], [rec("a", "pass")]).newly_passing == ["a@litellm"]


# ----------------------------------------------------------------- the three easy ones to misread


def test_a_case_that_stopped_running_is_lost_evidence_not_a_failure():
    """Calling this a regression would train people to ignore the alert."""
    d = compare([rec("a", "pass")], [rec("a", "skip", skip_class="throttled")])
    assert d.newly_failing == []
    assert d.stopped_running == ["a@litellm (throttled)"]


def test_the_reason_a_case_stopped_running_is_named():
    """`excluded_by_policy` and `throttled` are different problems with different fixes."""
    d = compare([rec("a", "pass")], [rec("a", "skip", skip_class="excluded_by_policy")])
    assert "excluded_by_policy" in d.stopped_running[0]


def test_a_skipped_case_that_now_fails_is_new_evidence_not_a_regression():
    """The fleet did not change; we started looking. But it must not hide in a neutral count."""
    d = compare([rec("a", "skip", skip_class="excluded_by_policy")], [rec("a", "fail")])
    assert d.newly_failing == [], "the fleet did not get worse"
    assert d.started_failing == ["a@litellm"], "but this is not nothing either"
    assert not d.regressed


def test_a_skipped_case_that_now_passes_is_separated_from_one_that_now_fails():
    d = compare(
        [rec("a", "skip", skip_class="x"), rec("b", "skip", skip_class="x")],
        [rec("a", "pass"), rec("b", "fail")],
    )
    assert d.started_passing == ["a@litellm"] and d.started_failing == ["b@litellm"]


# ----------------------------------------------------------------- identity


def test_the_same_case_at_two_layers_is_two_cases():
    """A stack can regress at the gateway while the engine is untouched."""
    d = compare(
        [rec("a", "pass", ingress="litellm"), rec("a", "pass", ingress="epp")],
        [rec("a", "fail", ingress="litellm"), rec("a", "pass", ingress="epp")],
    )
    assert d.newly_failing == ["a@litellm"]
    assert d.still_passing == ["a@epp"]


def test_variants_of_one_case_are_tracked_separately():
    d = compare(
        [rec("a", "pass", variant="messages"), rec("a", "pass", variant="responses")],
        [rec("a", "fail", variant="messages"), rec("a", "pass", variant="responses")],
    )
    assert d.newly_failing == ["a[messages]@litellm"]


def test_suite_churn_is_never_a_fleet_change():
    d = compare([rec("gone", "pass")], [rec("fresh", "fail")])
    assert d.appeared == ["fresh@litellm"] and d.vanished == ["gone@litellm"]
    assert d.newly_failing == [], "a case that did not exist before cannot have regressed"


def test_a_changed_profile_digest_is_detectable():
    assert digests([rec("a", "pass", digest="d1")]) != digests([rec("a", "pass", digest="d2")])


def test_key_falls_back_to_nodeid_when_a_record_has_no_case_id():
    assert key({"nodeid": "t.py::test_x", "ingress": "epp"}) == ("epp", "t.py::test_x", "")


# ----------------------------------------------------------------- the message


def test_the_headline_leads_with_the_regression_count():
    d = compare([rec("a", "pass")], [rec("a", "fail")])
    assert render(d, "p", "r1", "r2").splitlines()[0].startswith("*p* — 1 newly failing")


def test_a_clean_comparison_says_so_rather_than_printing_empty_sections():
    text = render(compare([rec("a", "pass")], [rec("a", "pass")]), "p", "r1", "r2")
    assert "unchanged, nothing failing" in text
    assert "Newly failing" not in text


def test_a_changed_profile_is_called_out_before_the_findings():
    d = compare([rec("a", "pass")], [rec("a", "fail")])
    d.digest_changed = True
    text = render(d, "p", "r1", "r2")
    assert "profile changed" in text
    assert text.index("profile changed") < text.index("Newly failing")


def test_long_lists_are_capped_so_the_message_stays_pasteable():
    before = [rec(f"c{i}", "pass") for i in range(40)]
    after = [rec(f"c{i}", "fail") for i in range(40)]
    text = render(compare(before, after), "p", "r1", "r2")
    assert "… and 30 more" in text


# ----------------------------------------------------------------- reading the archive

import json  # noqa: E402

import pytest  # noqa: E402

from llmdv.cli.main import main  # noqa: E402
from llmdv.compare.archive import (  # noqa: E402
    NoSuchRun,
    list_runs,
    pick_pair,
    read_results,
)
from llmdv.output import manifest  # noqa: E402


def archive(tmp_path, profile="p", runs=None, triggers=None):
    """A published-run layout: <root>/<profile>/<run-id>/results.jsonl."""
    for run_id, records in (runs or {}).items():
        d = tmp_path / profile / run_id
        d.mkdir(parents=True)
        (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in records))
        trigger = (triggers or {}).get(run_id, "nightly")
        labels = {} if trigger is None else {"trigger": trigger}
        (d / "outcome.json").write_text(json.dumps({"labels": labels}))
        manifest.write(d)
    return f"file://{tmp_path}"


def test_runs_come_back_oldest_first(tmp_path):
    """Run ids start with a UTC timestamp, so lexical order is chronological."""
    uri = archive(tmp_path, runs={"2026-01-02T00-00-00Z": [], "2026-01-01T00-00-00Z": []})
    assert list_runs(uri, "p") == ["2026-01-01T00-00-00Z", "2026-01-02T00-00-00Z"]


def test_a_profile_with_no_runs_is_empty_not_an_error(tmp_path):
    assert list_runs(archive(tmp_path), "never-ran") == []


def test_a_directory_without_results_is_not_a_run(tmp_path):
    """A half-published run must not be offered as a baseline."""
    (tmp_path / "p" / "2026-01-01T00-00-00Z").mkdir(parents=True)
    assert list_runs(f"file://{tmp_path}", "p") == []


def test_results_are_read_back_as_records(tmp_path):
    uri = archive(tmp_path, runs={"r1": [rec("a", "pass")]})
    assert read_results(uri, "p", "r1")[0]["case_id"] == "a"


def test_reading_a_missing_run_says_which_one(tmp_path):
    with pytest.raises(NoSuchRun, match="r9"):
        read_results(archive(tmp_path), "p", "r9")


def test_the_default_pair_is_the_two_most_recent():
    assert pick_pair(["r1", "r2", "r3"]) == ("r2", "r3")


def test_a_named_baseline_is_compared_against_the_latest():
    assert pick_pair(["r1", "r2", "r3"], baseline="r1") == ("r1", "r3")


def test_a_single_run_refuses_rather_than_reporting_a_clean_comparison():
    """A comparison against nothing is not a pass, and must never be mistaken for one."""
    with pytest.raises(NoSuchRun, match="only one run"):
        pick_pair(["r1"])


def test_a_baseline_that_is_already_the_latest_run_is_refused():
    with pytest.raises(NoSuchRun, match="latest run"):
        pick_pair(["r1", "r2"], baseline="r2")


# ----------------------------------------------------------------- the command


def test_a_regression_exits_nonzero_so_a_scheduled_job_can_gate_on_it(tmp_path):
    uri = archive(
        tmp_path, runs={"2026-01-01T00Z": [rec("a", "pass")], "2026-01-02T00Z": [rec("a", "fail")]}
    )
    assert main(["compare", "--from", uri, "--profile", "p"]) == 1


def test_no_fail_reports_the_same_regression_but_exits_clean(tmp_path):
    uri = archive(
        tmp_path, runs={"2026-01-01T00Z": [rec("a", "pass")], "2026-01-02T00Z": [rec("a", "fail")]}
    )
    assert main(["compare", "--from", uri, "--profile", "p", "--no-fail"]) == 0


def test_an_unchanged_fleet_exits_clean(tmp_path):
    uri = archive(
        tmp_path, runs={"2026-01-01T00Z": [rec("a", "pass")], "2026-01-02T00Z": [rec("a", "pass")]}
    )
    assert main(["compare", "--from", uri, "--profile", "p"]) == 0


def test_nothing_to_compare_is_a_usage_error_not_a_pass(tmp_path):
    """Exit 0 here would report a first run, or a typo'd profile, as a healthy fleet."""
    uri = archive(tmp_path, runs={"2026-01-01T00Z": [rec("a", "pass")]})
    assert main(["compare", "--from", uri, "--profile", "p"]) == 2
    assert main(["compare", "--from", uri, "--profile", "typo"]) == 2


# ----------------------------------------------------------------- zero cases is not a pass

from llmdv.compare.archive import EmptyRun, require_records  # noqa: E402


def test_a_run_that_recorded_nothing_is_refused_not_reported_clean():
    """An OOM kill, a crash or a selector that matched nothing all produce this shape."""
    with pytest.raises(EmptyRun, match="nothing to compare"):
        require_records([], "r2")


def test_a_collapsed_later_run_does_not_exit_clean(tmp_path):
    """Without this the five vanished cases read as a suite change and the run reports green."""
    uri = archive(
        tmp_path,
        runs={"2026-01-01T00Z": [rec(f"c{i}", "pass") for i in range(5)], "2026-01-02T00Z": []},
    )
    assert main(["compare", "--from", uri, "--profile", "p"]) == 2


def test_two_empty_runs_are_refused_rather_than_called_unchanged(tmp_path):
    uri = archive(tmp_path, runs={"2026-01-01T00Z": [], "2026-01-02T00Z": []})
    assert main(["compare", "--from", uri, "--profile", "p"]) == 2


def test_losing_most_cases_warns_that_a_crash_looks_like_this():
    before = [rec(f"c{i}", "pass") for i in range(20)]
    text = render(compare(before, before[:2]), "p", "r1", "r2")
    assert "crashed or mis-selected" in text


def test_ordinary_churn_does_not_cry_crash():
    before = [rec(f"c{i}", "pass") for i in range(20)]
    text = render(compare(before, before[:18]), "p", "r1", "r2")
    assert "crashed" not in text
    assert "2 case(s) removed" in text


def test_added_cases_alone_are_still_attributed_to_the_suite():
    text = render(compare([rec("a", "pass")], [rec("a", "pass"), rec("b", "pass")]), "p", "1", "2")
    assert "That is us, not the fleet" in text


# ----------------------------------------------------------------- the job handoff

from llmdv import notify  # noqa: E402
from llmdv.cli.compare_cmd import compare_run  # noqa: E402
from llmdv.compare.report import COMPARISON_FILE  # noqa: E402


def test_a_first_run_does_not_fail_the_job_it_belongs_to(tmp_path):
    """Night one has nothing to compare against. That is not a regression."""
    uri = archive(tmp_path / "a")
    run_dir = _run_dir(tmp_path, [rec("a", "pass")])
    code, text = compare_run(run_dir, uri, "p")
    assert code == 0
    assert text.startswith("Comparison unavailable:") and "no earlier nightly run" in text
    assert (run_dir / COMPARISON_FILE).read_text() == text + "\n"


def test_a_regression_is_written_beside_the_results_and_reported(tmp_path):
    uri = archive(tmp_path / "a", runs={"2026-01-01T00Z": [rec("a", "pass")]})
    run_dir = tmp_path / "run" / "2026-01-02T00Z"
    run_dir.mkdir(parents=True)
    (run_dir / "results.jsonl").write_text(json.dumps(rec("a", "fail")) + "\n")
    code, text = compare_run(run_dir, uri, "p")
    assert code == 1, "a regression must be able to fail the job"
    assert "Newly failing" in (run_dir / COMPARISON_FILE).read_text()
    assert "Newly failing" in text


def test_an_on_demand_subset_is_not_the_next_nightly_baseline(tmp_path):
    arch = tmp_path / "archive"
    uri = archive(
        arch,
        runs={
            "2026-01-01T00Z": [rec("a", "pass"), rec("outside", "pass")],
            "2026-01-02T00Z": [rec("a", "pass")],
        },
        triggers={"2026-01-02T00Z": "manual"},
    )
    current = _run_dir(
        tmp_path / "local", [rec("a", "pass"), rec("outside", "fail")], name="2026-01-03T00Z"
    )
    code, text = compare_run(current, uri, "p")
    assert code == 1
    assert "Newly failing" in text and "outside@litellm" in text


def test_an_on_demand_comparison_uses_the_latest_nightly(tmp_path):
    uri = archive(
        tmp_path / "archive",
        runs={"r1": [rec("a", "pass")], "r2": [rec("a", "fail")]},
        triggers={"r2": "manual"},
    )
    current = _run_dir(tmp_path, [dict(rec("a", "fail"), run_id="r3")], name="r3")
    code, text = compare_run(current, uri, "p")
    assert code == 1 and "`r1` → `r3`" in text


def test_a_window_counts_only_nightly_runs(tmp_path):
    uri = archive(
        tmp_path / "archive",
        runs={
            "r1": [rec("a", "pass")],
            "r2": [rec("a", "fail")],
            "r3": [rec("a", "pass")],
            "r4": [rec("a", "fail")],
            "r5": [rec("a", "pass")],
        },
        triggers={"r2": "manual", "r4": "manual"},
    )
    current = _run_dir(tmp_path, [dict(rec("a", "fail"), run_id="r6")], name="r6")
    code, text = compare_run(current, uri, "p", window=3)
    assert code == 1
    assert "`r5` → `r6`" in text and "last 3 runs" in text


def test_an_explicit_baseline_may_be_non_nightly(tmp_path):
    uri = archive(
        tmp_path / "archive",
        runs={"r1": [rec("a", "pass")], "r2": [rec("a", "fail")]},
        triggers={"r2": "manual"},
    )
    current = _run_dir(tmp_path, [dict(rec("a", "fail"), run_id="r3")], name="r3")
    code, text = compare_run(current, uri, "p", baseline="r2")
    assert code == 0 and "`r2` → `r3`" in text


def test_a_run_without_a_trigger_label_is_not_a_baseline(tmp_path):
    uri = archive(tmp_path / "archive", runs={"r1": [rec("a", "pass")]}, triggers={"r1": None})
    current = _run_dir(tmp_path, [rec("a", "pass")])
    code, text = compare_run(current, uri, "p")
    assert code == 0 and "no earlier nightly run" in text


def test_the_comparison_lands_in_the_run_directory_the_uploader_walks(tmp_path):
    """Renamed, because the old name claimed publication and the body only checked a local
    file -- while the comparison was in fact written after the upload and never shipped."""
    uri = archive(tmp_path / "a", runs={"2026-01-01T00Z": [rec("a", "pass")]})
    run_dir = tmp_path / "run" / "2026-01-02T00Z"
    run_dir.mkdir(parents=True)
    (run_dir / "results.jsonl").write_text(json.dumps(rec("a", "pass")) + "\n")
    compare_run(run_dir, uri, "p")
    assert (run_dir / COMPARISON_FILE).is_file()


def test_the_comparison_is_in_place_before_anything_is_published(tmp_path):
    """The real guarantee: by the time a publisher walks the run directory, it is there."""
    uri = archive(tmp_path / "a", runs={"2026-01-01T00Z": [rec("a", "pass")]})
    run_dir = tmp_path / "run" / "2026-01-02T00Z"
    run_dir.mkdir(parents=True)
    (run_dir / "results.jsonl").write_text(json.dumps(rec("a", "pass")) + "\n")
    compare_run(run_dir, uri, "p")
    walked = {f.name for f in run_dir.iterdir()}
    assert COMPARISON_FILE in walked, "an uploader walking the directory now would ship it"


def test_the_notification_carries_the_comparison_when_one_was_made():
    outcome = {"run_id": "r2", "profile": {"name": "p"}, "verification": {"exit_code": 0}}
    text = notify.message(outcome, 0, "*p* — 2 newly failing\n  • `a@g`")
    assert "2 newly failing" in text
    assert text.index("2 newly failing") < text.index("run r2"), "delta sits above the footer"


def test_a_notification_without_a_comparison_is_unchanged():
    outcome = {"run_id": "r2", "profile": {"name": "p"}, "verification": {"exit_code": 0}}
    assert "newly failing" not in notify.message(outcome, 0)


# --------------------------------------------------- what a nightly job depends on


from llmdv.compare.archive import pick_baseline  # noqa: E402


def _run_dir(tmp_path, records, name="2026-01-03T00Z"):
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return d


def test_an_unreachable_archive_never_costs_the_notification(tmp_path, monkeypatch):
    """`llmdv job` promises a notification even when publication fails. A comparison that raised
    took the job down with exit 4 before notify_run was reached -- on exactly the night the
    store had a problem."""
    from llmdv.cli import compare_cmd

    monkeypatch.setattr(
        compare_cmd,
        "list_runs",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("endpoint unreachable")),
    )
    current = _run_dir(tmp_path, [rec("a", "pass")])
    code, text = compare_cmd.compare_run(current, "s3://b/p", "p")
    assert code == 0
    assert text == "Comparison unavailable: endpoint unreachable"
    assert (current / COMPARISON_FILE).read_text() == text + "\n"


@pytest.mark.parametrize(
    "exc",
    [ConnectionError("refused"), RuntimeError("InvalidAccessKeyId"), KeyError("NoSuchBucket")],
)
def test_any_archive_failure_degrades_to_no_comparison(tmp_path, monkeypatch, exc):
    from llmdv.cli import compare_cmd

    monkeypatch.setattr(compare_cmd, "list_runs", lambda *a, _e=exc, **k: (_ for _ in ()).throw(_e))
    code, _ = compare_cmd.compare_run(_run_dir(tmp_path, [rec("a", "pass")]), "s3://b/p", "p")
    assert code == 0, "a comparison is advisory; the run's verdict and notification are not"


def test_the_finishing_run_is_read_locally_not_from_the_archive(tmp_path):
    """If this run failed to publish, the archive's latest two are two OLDER runs, and their
    regression would be reported as tonight's."""
    arch = tmp_path / "arch"
    archive(
        arch,
        runs={
            # Same digest as the local run below: this test is about where the finishing run is
            # read from, not about contract drift, so the contracts must agree.
            "2026-01-01T00Z": [rec("a", "pass")],
            "2026-01-02T00Z": [rec("a", "fail")],
        },
    )
    tonight = _run_dir(tmp_path / "local", [dict(rec("a", "pass"), run_id="2026-01-03T00Z")])
    code, text = compare_run(tonight, f"file://{arch}", "p")
    assert code == 0, "tonight passed; the old regression is not tonight's news"
    # The point is only where the finishing run came from. How a pass-after-fail is phrased
    # depends on the window and is covered by the window tests, so this asserts neither.
    assert "Newly failing" not in text, "the archive's own regression is not attributed to tonight"
    assert "2026-01-03T00Z" in text, "tonight's run is the one being judged"


def test_the_comparison_is_written_before_publication(tmp_path):
    """It has to exist in the run directory when the uploader walks it, or it never ships."""
    arch = tmp_path / "arch"
    archive(arch, runs={"2026-01-01T00Z": [rec("a", "pass")]})
    tonight = _run_dir(tmp_path / "local", [dict(rec("a", "pass"), run_id="2026-01-03T00Z")])
    compare_run(tonight, f"file://{arch}", "p")
    assert (tonight / COMPARISON_FILE).is_file()


def test_a_run_already_in_the_archive_is_not_compared_to_itself():
    """A rerun, or a comparison made after publishing, would otherwise report a clean diff."""
    assert pick_baseline(["r1", "r2"], exclude="r2") == "r1"


def test_the_baseline_is_the_latest_earlier_run():
    assert pick_baseline(["r1", "r2", "r3"], exclude="r3") == "r2"


def test_an_archive_with_only_this_run_has_no_baseline():
    with pytest.raises(NoSuchRun, match="no earlier run"):
        pick_baseline(["r1"], exclude="r1")


def test_an_explicit_baseline_must_exist_in_the_archive():
    with pytest.raises(NoSuchRun, match="not found"):
        pick_baseline(["r1", "r2"], exclude="r2", baseline="nope")


def test_the_comparison_is_uploaded_on_its_own(tmp_path):
    """`llmdv job` hands --remote-output to the verify child, so the run uploads itself before the
    comparison exists. Reordering the job is not enough -- the file has to be sent separately or
    it never leaves the pod, which is what the first version of this did."""
    from llmdv.compare.archive import publish_one

    arch = tmp_path / "arch"
    archive(arch, runs={"2026-01-01T00Z": [rec("a", "pass")]})
    run_dir = tmp_path / "run" / "2026-01-02T00Z"
    run_dir.mkdir(parents=True)
    (run_dir / "results.jsonl").write_text(
        json.dumps(dict(rec("a", "fail"), run_id="2026-01-02T00Z")) + "\n"
    )
    compare_run(run_dir, f"file://{arch}", "p")
    assert (arch / "p" / "2026-01-02T00Z" / COMPARISON_FILE).is_file(), (
        "the comparison must reach the archive, not only the run directory"
    )
    # and the helper itself, independent of the caller
    src = tmp_path / "x.md"
    src.write_text("hello")
    publish_one(f"file://{arch}", "p", "2026-01-01T00Z", src)
    assert (arch / "p" / "2026-01-01T00Z" / "x.md").read_text() == "hello"


def test_a_failed_upload_of_the_comparison_does_not_fail_the_job(tmp_path, monkeypatch):
    """Advisory: the analysis is in the notification too."""
    from llmdv.cli import compare_cmd

    arch = tmp_path / "arch"
    archive(arch, runs={"2026-01-01T00Z": [rec("a", "pass")]})
    run_dir = tmp_path / "run" / "2026-01-02T00Z"
    run_dir.mkdir(parents=True)
    (run_dir / "results.jsonl").write_text(
        json.dumps(dict(rec("a", "pass"), run_id="2026-01-02T00Z")) + "\n"
    )
    monkeypatch.setattr(
        compare_cmd, "publish_one", lambda *a, **k: (_ for _ in ()).throw(OSError("store gone"))
    )
    code, _ = compare_cmd.compare_run(run_dir, f"file://{arch}", "p")
    assert code == 0


def test_a_comparison_does_not_inherit_an_uploader_s_patience():
    """Delivering results is worth waiting for; reading to enrich a report is not. Sharing the
    uploader's 5 attempts at 10s/60s turned an unreachable store into a nightly that hung for
    minutes before giving up."""
    from llmdv.compare.archive import _s3
    from llmdv.output.remote import DELIVERY, GLANCE, parse_remote_uri
    from llmdv.output.s3 import S3Sink

    target = parse_remote_uri("s3://b/p")
    assert S3Sink(target).patience == DELIVERY, "publishing still waits"
    assert _s3(target).patience == GLANCE, "a comparison gives up quickly"
    assert GLANCE.attempts < DELIVERY.attempts
    assert GLANCE.connect < DELIVERY.connect and GLANCE.read < DELIVERY.read


# ----------------------------------------------------------------- the flake window

from llmdv.compare.archive import pick_window  # noqa: E402
from llmdv.compare.diff import compare_window  # noqa: E402


def _night(**outcomes):
    return [
        {"case_id": c, "ingress": "g", "outcome": o, "profile_digest": "d"}
        for c, o in outcomes.items()
    ]


def test_a_case_the_window_agrees_passed_is_a_real_regression():
    history = [_night(a="pass") for _ in range(5)]
    d = compare_window(history, _night(a="fail"))
    assert d.newly_failing == ["a@g"] and d.flaky == []


def test_a_case_the_window_disagrees_about_is_flaky_not_a_regression():
    """Measured flakiness exists -- an answer-budget case failed 1 run in 3. Paging on it is
    how a nightly gets muted."""
    history = [_night(a="pass"), _night(a="fail"), _night(a="pass")]
    d = compare_window(history, _night(a="fail"))
    assert d.newly_failing == [], "the window cannot tell a regression from a flap"
    assert len(d.flaky) == 1 and not d.regressed


def test_a_flaky_case_shows_its_pattern_oldest_first():
    history = [_night(a="pass"), _night(a="fail"), _night(a="pass")]
    assert "(.F.F)" in compare_window(history, _night(a="fail")).flaky[0], (
        "three nights then tonight"
    )


def test_a_consistently_failing_case_is_still_not_new():
    history = [_night(a="fail") for _ in range(3)]
    d = compare_window(history, _night(a="fail"))
    assert d.still_failing == ["a@g"] and d.flaky == []


def test_a_recovery_is_only_called_one_when_the_window_agreed_it_was_broken():
    history = [_night(a="fail") for _ in range(3)]
    assert compare_window(history, _night(a="pass")).newly_passing == ["a@g"]


def test_runs_where_the_case_was_skipped_do_not_count_as_verdicts():
    """A skip says nothing about the fleet, so it must not dilute the window's agreement."""
    history = [
        _night(a="pass"),
        [{"case_id": "a", "ingress": "g", "outcome": "skip", "skip_class": "throttled"}],
        _night(a="pass"),
    ]
    assert compare_window(history, _night(a="fail")).newly_failing == ["a@g"]


def test_a_case_skipped_through_the_whole_window_is_new_evidence():
    history = [[{"case_id": "a", "ingress": "g", "outcome": "skip", "skip_class": "x"}]] * 3
    d = compare_window(history, _night(a="fail"))
    assert d.started_failing == ["a@g"] and d.newly_failing == []


def test_a_case_absent_from_the_whole_window_is_new_to_the_suite():
    assert compare_window([_night(a="pass")], _night(a="pass", b="fail")).appeared == ["b@g"]


def test_the_window_reports_how_many_runs_it_used():
    assert compare_window([_night(a="pass")] * 3, _night(a="pass")).window == 3


def test_one_run_of_history_behaves_as_the_old_two_run_comparison():
    assert compare_window([_night(a="pass")], _night(a="fail")).newly_failing == ["a@g"]


def test_the_window_takes_the_most_recent_runs():
    assert pick_window(["r1", "r2", "r3", "r4"], size=2) == ["r3", "r4"]


def test_a_short_archive_uses_what_it_has():
    assert pick_window(["r1", "r2"], size=5) == ["r1", "r2"]


def test_an_explicit_baseline_overrides_the_window():
    """Naming the run from before a rollout means that run, not a consensus around it."""
    assert pick_window(["r1", "r2", "r3"], baseline="r1", size=5) == ["r1"]


def test_the_finishing_run_is_never_part_of_its_own_window():
    assert pick_window(["r1", "r2"], exclude="r2", size=5) == ["r1"]


# ----------------------------------------------------------------- drift, and safety

from llmdv.compare.archive import (  # noqa: E402
    UnsafeName,
    declaration_diff,
    read_profile,
    safe,
)


def test_the_report_names_the_declaration_that_moved():
    """ "The profile changed" leaves a reader to diff two files by hand. One tightened line can
    account for every case that moved."""
    before = "headers:\n  x-request-id:\n    litellm: ignored\n"
    after = "headers:\n  x-request-id:\n    litellm: echoed\n"
    changed = declaration_diff(before, after)
    assert any(line.startswith("-") and "ignored" in line for line in changed)
    assert any(line.startswith("+") and "echoed" in line for line in changed)


def test_comments_and_blank_lines_are_not_declaration_changes():
    """Profiles carry long measurement notes; rewording one is not a contract change."""
    before = "# measured on tuesday\nmodel: m\n"
    after = "# measured on wednesday, see ticket\n\nmodel: m\n"
    assert declaration_diff(before, after) == []


def test_a_missing_profile_yields_no_diff_rather_than_an_error():
    assert declaration_diff(None, "model: m") == []


def test_a_long_declaration_diff_is_capped():
    before = "\n".join(f"k{i}: a" for i in range(40))
    after = "\n".join(f"k{i}: b" for i in range(40))
    out = declaration_diff(before, after, cap=6)
    assert len(out) == 7 and out[-1].startswith("… and")


def test_an_unreadable_profile_is_not_fatal(tmp_path):
    assert read_profile(f"file://{tmp_path}", "p", "nope") is None


@pytest.mark.parametrize("bad", ["../secret", "a/b", "..", ".hidden", "", "x\\y"])
def test_a_name_that_could_leave_the_archive_is_refused(bad):
    """Profile names come from a field somebody writes in YAML. On a shared bucket
    `../other-team` would read another team's runs, and publishing would write into them."""
    with pytest.raises(UnsafeName):
        safe(bad, "profile name")


def test_ordinary_names_are_untouched():
    assert safe("example-litellm", "profile name") == "example-litellm"
    assert safe("2026-10-07T20-45-37Z-d130", "run id") == "2026-10-07T20-45-37Z-d130"


def test_listing_refuses_a_traversing_profile_name(tmp_path):
    with pytest.raises(UnsafeName):
        list_runs(f"file://{tmp_path}", "../elsewhere")


def test_the_window_refuses_to_mix_contracts():
    """A verdict means something only against the declarations it was made under. A window
    spanning a profile change would otherwise count "every run agreed it passed" as agreement
    about a contract that no longer applies."""

    def night(outcome, digest):
        return [{"case_id": "a", "ingress": "g", "outcome": outcome, "profile_digest": digest}]

    d = compare_window(
        [night("pass", "OLD"), night("pass", "OLD"), night("pass", "NEW")], night("fail", "NEW")
    )
    assert d.window == 1, "only the run sharing tonight's contract counts"
    assert d.window_dropped == 2


def test_a_shortened_window_is_not_the_same_as_a_moved_baseline():
    """Two different facts. Saying the profile changed 'between these runs' when the baseline
    matches would send a reader looking for a change that is not there."""

    def night(outcome, digest):
        return [{"case_id": "a", "ingress": "g", "outcome": outcome, "profile_digest": digest}]

    earlier_drift = compare_window(
        [night("pass", "OLD"), night("pass", "NEW")], night("fail", "NEW")
    )
    assert earlier_drift.window_dropped == 1
    assert not earlier_drift.digest_changed, "the baseline agrees with tonight"

    baseline_drift = compare_window([night("pass", "OLD")], night("fail", "NEW"))
    assert baseline_drift.digest_changed, "the baseline does not"
    assert baseline_drift.window_dropped == 0, "a run we fell back to was not left out"


def test_a_window_with_no_matching_contract_still_compares_against_the_newest():
    """Refusing to compare at all would hide a regression behind a profile edit."""

    def night(outcome, digest):
        return [{"case_id": "a", "ingress": "g", "outcome": outcome, "profile_digest": digest}]

    d = compare_window([night("pass", "OLD"), night("pass", "OLD")], night("fail", "NEW"))
    assert d.newly_failing == ["a@g"] and d.digest_changed


def test_runs_without_a_recorded_digest_are_not_excluded():
    """Older archives may predate the field; absence is not disagreement."""
    d = compare_window(
        [[{"case_id": "a", "ingress": "g", "outcome": "pass"}]],
        [{"case_id": "a", "ingress": "g", "outcome": "fail", "profile_digest": "NEW"}],
    )
    assert d.newly_failing == ["a@g"] and d.window_dropped == 0


# --------------------------------------------------- truthful headlines and complete archives


@pytest.mark.parametrize("outcome", ["fail", "error"])
@pytest.mark.parametrize("previous", ["skip", None])
def test_newly_observed_failures_are_visible_in_the_headline(previous, outcome):
    before = [rec("existing", "pass")]
    if previous:
        before.append(rec("new", previous))
    d = compare(before, [rec("existing", "pass"), rec("new", outcome)])
    text = render(d, "p", "r1", "r2")
    assert text.splitlines()[0] == "*p* — 1 failing, no new regressions"
    assert "new@litellm" in text
    assert not d.regressed


def test_recoveries_do_not_hide_other_current_failures_in_the_headline():
    d = compare(
        [rec("recovered", "fail"), rec("started", "skip"), rec("broken", "fail")],
        [rec("recovered", "pass"), rec("started", "fail"), rec("broken", "fail")],
    )
    assert render(d, "p", "r1", "r2").splitlines()[0] == ("*p* — 2 failing, no new regressions")
    assert d.newly_passing == ["recovered@litellm"]


@pytest.mark.parametrize("now", ["pass", "fail"])
def test_flakiness_does_not_hide_whether_the_case_is_failing_now(now):
    d = compare_window([_night(a="pass"), _night(a="fail")], _night(a=now))
    headline = render(d, "p", "r1", "r2").splitlines()[0]
    assert ("1 failing" in headline) is (now == "fail")
    assert d.flaky and not d.regressed


def test_an_unfinished_upload_cannot_hide_a_regression(tmp_path):
    arch = tmp_path / "archive"
    uri = archive(arch, runs={"r1": [rec("a", "pass")], "r2": [rec("a", "fail")]})
    (arch / "p" / "r2" / manifest.FILENAME).unlink()
    assert list_runs(uri, "p") == ["r1"]
    current = _run_dir(tmp_path, [dict(rec("a", "fail"), run_id="r3")], name="r3")
    code, text = compare_run(current, uri, "p")
    assert code == 1
    assert "`r1` → `r3`" in text
    with pytest.raises(NoSuchRun, match="not found"):
        pick_window(list_runs(uri, "p"), baseline="r2")


@pytest.mark.parametrize("stage", ["compare_window", "declaration_diff", "render"])
def test_analysis_failures_degrade_to_no_comparison(tmp_path, monkeypatch, stage):
    from llmdv.cli import compare_cmd

    uri = archive(tmp_path / "archive", runs={"r1": [rec("a", "pass", digest="old")]})
    current = _run_dir(tmp_path, [rec("a", "pass", digest="new")])

    def broken(*args, **kwargs):
        raise RuntimeError("comparison analysis failed")

    monkeypatch.setattr(compare_cmd, stage, broken)
    code, text = compare_run(current, uri, "p")
    assert code == 0
    assert text == "Comparison unavailable: comparison analysis failed"
    assert (current / COMPARISON_FILE).read_text() == text + "\n"


@pytest.mark.parametrize("side", ["archive", "local"])
@pytest.mark.parametrize(
    "bad_row", ["{", "null", "[]", '{"case_id": "a"}', '{"outcome": "unknown"}']
)
def test_malformed_results_never_produce_a_clean_comparison(tmp_path, side, bad_row):
    arch = tmp_path / "archive"
    uri = archive(arch, runs={"r1": [rec("a", "pass")]})
    current = _run_dir(tmp_path, [rec("a", "pass")])
    path = arch / "p" / "r1" if side == "archive" else current
    # A valid row before the corruption must not let a tolerant reader report a clean diff.
    with (path / "results.jsonl").open("a") as handle:
        handle.write("\n" + bad_row + "\n")
    code, text = compare_run(current, uri, "p")
    assert code == 0
    assert text.startswith("Comparison unavailable:")
    assert f"line {2 if side == 'archive' else 3}" in text
    assert (current / COMPARISON_FILE).read_text() == text + "\n"


@pytest.mark.parametrize("bad_id, good_id", [("r1", "r2"), ("r2", "r1")])
def test_a_malformed_baseline_is_dropped_without_hiding_the_regression(
    tmp_path, monkeypatch, bad_id, good_id
):
    from llmdv.cli import compare_cmd

    arch = tmp_path / "archive"
    uri = archive(
        arch,
        runs={"r1": [rec("a", "pass")], "r2": [rec("a", "pass")], "r3": [rec("a", "fail")]},
    )
    # Discard the whole corrupt run, not just its broken line.
    (arch / "p" / bad_id / "results.jsonl").write_text(json.dumps(rec("a", "fail")) + "\n{\n")
    current = _run_dir(tmp_path, [dict(rec("a", "fail"), run_id="r3")])
    code, text = compare_run(current, uri, "p", window=2)
    assert code == 1
    assert f"`{good_id}` → `r3`" in text
    assert "1 earlier run(s) left out" in text
    assert f"run {bad_id}, line 2: invalid JSON" in text
    assert "different profile" not in text

    lines = []
    monkeypatch.setattr(compare_cmd, "emit", lines.append)
    assert main(["compare", "--from", uri, "--profile", "p", "--window", "2"]) == 1
    output = "\n".join(lines)
    assert f"`{good_id}` → `r3`" in output
    assert f"run {bad_id}, line 2: invalid JSON" in output


def test_corruption_and_profile_drift_count_as_separate_reasons_for_dropping_runs(tmp_path):
    uri = archive(
        tmp_path / "archive",
        runs={
            "r1": [None],
            "r2": [rec("a", "fail", digest="old")],
            "r3": [rec("a", "pass")],
        },
    )
    current = _run_dir(tmp_path, [dict(rec("a", "fail"), run_id="r4")])
    code, text = compare_run(current, uri, "p", window=3)
    assert code == 1
    assert "2 earlier run(s) left out" in text
    assert "1 made under a different profile" in text
    assert "run r1, line 1" in text
    assert "`r3` → `r4`" in text


def test_an_entirely_unusable_window_reports_each_dropped_run(tmp_path):
    uri = archive(tmp_path / "archive", runs={"r1": [None], "r2": []})
    current = _run_dir(tmp_path, [dict(rec("a", "fail"), run_id="r3")])
    code, text = compare_run(current, uri, "p", window=2)
    assert code == 0
    assert text.startswith("Comparison unavailable:")
    assert "run r1, line 1" in text and "run r2 recorded no results" in text
    assert (current / COMPARISON_FILE).read_text() == text + "\n"


def test_an_explicit_corrupt_baseline_does_not_fall_back_to_another_run(tmp_path):
    uri = archive(
        tmp_path / "archive",
        runs={"r1": [rec("a", "pass")], "r2": [None], "r3": [rec("a", "fail")]},
    )
    current = _run_dir(tmp_path, [dict(rec("a", "fail"), run_id="r3")])
    code, text = compare_run(current, uri, "p", baseline="r2", window=2)
    assert code == 0
    assert text.startswith("Comparison unavailable:") and "run r2, line 1" in text
    assert main(["compare", "--from", uri, "--profile", "p", "--baseline", "r2"]) == 2


def test_unavailable_comparison_is_published_even_when_local_results_are_corrupt(tmp_path):
    from llmdv.evidence.outcome import Outcome

    arch = tmp_path / "archive"
    uri = archive(arch, runs={"r1": [rec("a", "pass")]})
    current = _run_dir(tmp_path, [None], name="job-directory")
    Outcome(run_id="actual-run-id").write(current)
    code, text = compare_run(current, uri, "p")
    assert code == 0 and text.startswith("Comparison unavailable:")
    assert (arch / "p" / "actual-run-id" / COMPARISON_FILE).read_text() == text + "\n"
    assert not (arch / "p" / "job-directory").exists()


def test_unavailable_comparison_scrubs_the_reason_before_writing_and_publishing(
    tmp_path, monkeypatch
):
    from llmdv.cli import compare_cmd
    from llmdv.evidence.outcome import Outcome
    from llmdv.http.redact import SENTINEL, install_secrets, reset_secrets

    secret = "test-comparison-secret"
    install_secrets([secret])
    uri = archive(tmp_path / "archive")
    current = _run_dir(tmp_path, [rec("a", "pass")])
    Outcome(run_id="r2").write(current)

    def broken(*args):
        raise ConnectionError(f"refused {secret}\ntry again")

    monkeypatch.setattr(compare_cmd, "list_runs", broken)
    try:
        code, text = compare_run(current, uri, "p")
        assert code == 0
        assert text == f"Comparison unavailable: refused {SENTINEL} try again"
        assert (current / COMPARISON_FILE).read_text() == text + "\n"
        assert (tmp_path / "archive" / "p" / "r2" / COMPARISON_FILE).read_text() == text + "\n"
    finally:
        reset_secrets()


def test_explicit_compare_rejects_malformed_results_as_a_usage_error(tmp_path):
    uri = archive(tmp_path, runs={"r1": [rec("a", "pass")], "r2": [{"case_id": "a"}]})
    assert main(["compare", "--from", uri, "--profile", "p"]) == 2


def test_s3_listing_requires_results_and_manifest_for_the_same_run(monkeypatch):
    pytest.importorskip("boto3")
    from tests.fakes import FakeS3, s3_env

    with FakeS3() as store:
        for name, value in s3_env(store).items():
            monkeypatch.setenv(name, value)
        data = json.dumps(rec("a", "pass")).encode()
        store.objects.update(
            {
                "results/nightly/p/r1/results.jsonl": data,
                "results/nightly/p/r1/MANIFEST.json": b"{}",
                "results/nightly/p/r2/results.jsonl": data,
                "results/nightly/p/r3/MANIFEST.json": b"{}",
                "results/nightly/p/r4/comparison.md": b"advisory only",
                "results/nightly/p/r5/nested/results.jsonl": data,
                "results/nightly/p/r5/nested/MANIFEST.json": b"{}",
            }
        )
        assert list_runs("s3://results/nightly", "p") == ["r1"]
        assert read_results("s3://results/nightly", "p", "r1") == [rec("a", "pass")]


# ------------------------------------------------------- failures that got no HTTP answer


def _failed(case, reason, status=None):
    r = rec(case, "fail")
    r["reason"] = reason
    r["http_status"] = status
    return r


def test_a_timeout_on_a_case_that_passed_is_a_regression_and_named_transient():
    after = [
        _failed("a", "AssertionError: stream failed: ReadTimeout: The read operation timed out")
    ]
    d = compare([rec("a", "pass")], after)
    assert d.newly_failing == ["a@litellm"], "an outage must never hide under 'flaky'"
    assert d.transient == ["a@litellm"]
    assert d.transitions[key(after[0])] == "regressed"


def test_a_5xx_is_a_defect_not_a_transient():
    after = [_failed("a", "AssertionError: expected a 4xx, got 500", status=500)]
    d = compare([rec("a", "pass")], after)
    assert d.newly_failing == ["a@litellm"] and d.transient == []


def test_a_dropped_stream_behind_a_200_is_transient_but_a_malformed_one_is_not():
    assert transport_failure(_failed("a", "stream failed: RemoteProtocolError: peer closed", 200))
    assert transport_failure(
        _failed("a", "stream failed: StreamIdleTimeout: no event for 120s", 200)
    )
    # llmdv's own "stream failed" phrase also wraps a stream that answered 200 with bad frames:
    # that recurs every run and must not be called transient.
    assert not transport_failure(
        _failed("a", "stream failed: StreamError: event 3 is not valid JSON: Expecting value", 200)
    )
    assert not transport_failure(_failed("a", "AssertionError: wrong answer", 200))
    assert not transport_failure(rec("a", "fail"))


def test_the_pinned_sdks_own_timeouts_are_transient():
    assert transport_failure(_failed("a", "openai.APITimeoutError: Request timed out."))
    assert transport_failure(_failed("a", "anthropic.APIConnectionError: Connection error."))


def test_a_transient_failure_is_named_under_the_regressions_and_in_the_record():
    after = [_failed("a", "transport failure: ConnectError: connection refused")]
    d = compare([rec("a", "pass")], after)
    text = render(d, "p", "r1", "r2")
    assert "1 newly failing" in text.splitlines()[0]
    assert "got no HTTP answer" in text and "rerun them before opening a finding" in text
    assert as_record(d, "r1", "r2")["transient"] == ["a@litellm"]


def test_a_case_that_kept_failing_is_not_called_transient():
    after = [_failed("a", "stream failed: ReadTimeout: timed out")]
    assert compare([rec("a", "fail")], after).transient == []
