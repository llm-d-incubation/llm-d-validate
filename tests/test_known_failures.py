"""A `known_failures` entry labels a failure the team has filed; it never softens the verdict.

The outcome stays `fail` and the exit code stays `1`. What changes is that the record, the
counts, the banner, `summary.md`, `verdict.txt` and the alert feed say the failure is known and
where the fix is. A case failing a check the entry does not name is a plain failure; a response
from the build the entry waited for turns the known failure back into a plain one; and a
comparison never calls a regression known.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from llmdv.cli import banner
from llmdv.compare.diff import compare
from llmdv.compare.report import as_record, render
from llmdv.evidence import alerts
from llmdv.evidence.outcome import known_summary
from llmdv.profile import loader
from llmdv.profile.known import at_or_past, classify, unmatched
from llmdv.profile.schema import KnownFailure
from tests.fakes import FakeFleet, profile_yaml, run_llmdv

ENTRY = KnownFailure(
    check="finish_reason", issue="#1", cases=("openai_chat.*",), ingresses=("fake",)
)
ISSUE = "example-org/serving-stack#1"

KNOWN_BLOCK = """
known_failures:
  - check: finish_reason
    cases: ["openai_chat.*"]
    ingress: fake
    issue: example-org/serving-stack#1
    fix: "the fake stops aborting"
"""


def failing(name: str) -> list[dict]:
    return [{"name": "http_status", "ok": True}, {"name": name, "ok": False}]


def fail_kw(**over) -> dict:
    kw = dict(outcome="fail", case_id="openai_chat.basic", variant=None, ingress="fake", signals=())
    kw.update(over)
    return kw


# ----------------------------------------------------------------- the matching rule


def test_the_entry_covers_only_a_failure_of_the_named_check():
    known = classify([ENTRY], checks=failing("finish_reason"), **fail_kw())
    assert known and known["status"] == "known" and known["issue"] == "#1"
    assert classify([ENTRY], checks=failing("http_status"), **fail_kw()) is None


def test_the_entry_is_scoped_by_case_glob_and_ingress():
    checks = failing("finish_reason")
    assert classify([ENTRY], checks=checks, **fail_kw(case_id="tools.basic")) is None
    assert classify([ENTRY], checks=checks, **fail_kw(ingress="router")) is None
    wide = KnownFailure(check="finish_*", issue="#1")
    assert classify([wide], checks=checks, **fail_kw(case_id="tools.basic", ingress="router"))


def test_a_variant_can_be_named_in_the_glob():
    entry = KnownFailure(
        check="field_status", issue="#1", cases=("openai_chat.request_field[logit_bias]",)
    )
    kw = fail_kw(case_id="openai_chat.request_field")
    assert classify([entry], checks=failing("field_status"), **{**kw, "variant": "logit_bias"})
    assert classify([entry], checks=failing("field_status"), **{**kw, "variant": "seed"}) is None


def test_a_failure_with_no_failed_check_is_never_known():
    """A Python error in the test body records no check; a label would hide a llmdv defect."""
    anything = KnownFailure(check="*x*", issue="#1")
    assert classify([anything], checks=[], **fail_kw(outcome="error")) is None
    # A failed check with no name cannot be declared either.
    assert classify([anything], checks=[{"ok": False}], **fail_kw()) is None


def test_a_pass_is_never_labelled():
    assert classify([ENTRY], checks=[], **fail_kw(outcome="pass")) is None
    assert classify([ENTRY], checks=[], **fail_kw(outcome="skip")) is None


def test_a_response_from_the_awaited_build_turns_the_label_into_fix_expected():
    entry = KnownFailure(check="finish_reason", issue="#1", until={"x-litellm-version": "1.104.1"})
    checks = failing("finish_reason")
    before = classify(
        [entry], checks=checks, **fail_kw(signals=[("fake", {"x-litellm-version": "1.103.0"})])
    )
    after = classify(
        [entry], checks=checks, **fail_kw(signals=[("fake", {"x-litellm-version": "1.104.1"})])
    )
    later = classify(
        [entry],
        checks=checks,
        **fail_kw(
            signals=[("fake", {"server": "nginx"}), ("fake", {"x-litellm-version": "1.110.0"})]
        ),
    )
    assert before["status"] == "known" and before["observed"] == {}
    assert after["status"] == "fix_expected"
    assert after["observed"] == {"x-litellm-version": "1.104.1"}
    assert later["status"] == "fix_expected"


def test_every_awaited_signal_must_have_answered_from_the_entrys_own_ingress():
    entry = KnownFailure(
        check="finish_reason",
        issue="#1",
        ingresses=("engine",),
        until={"x-litellm-version": "1.104.1", "system_fingerprint": "vllm-g2107750"},
    )
    checks = failing("finish_reason")
    both = {"x-litellm-version": "1.104.1", "system_fingerprint": "vllm-g2107750"}
    one = classify(
        [entry],
        checks=checks,
        **fail_kw(ingress="engine", signals=[("engine", {"x-litellm-version": "1.104.1"})]),
    )
    all_of = classify(
        [entry], checks=checks, **fail_kw(ingress="engine", signals=[("engine", both)])
    )
    # The gateway's headers in a cross-layer case must not speak for the engine.
    other_layer = classify(
        [entry], checks=checks, **fail_kw(ingress="engine", signals=[("gateway", both)])
    )
    assert one["status"] == "known"
    assert all_of["status"] == "fix_expected" and set(all_of["observed"]) == set(entry.until)
    assert other_layer["status"] == "known"


def test_the_most_specific_entry_wins_whatever_the_profile_order():
    broad = KnownFailure(check="finish_reason", issue="#broad")
    narrow = KnownFailure(check="finish_reason", issue="#narrow", cases=("openai_chat.*",))
    checks = failing("finish_reason")
    assert classify([broad, narrow], checks=checks, **fail_kw())["issue"] == "#narrow"
    assert classify([narrow, broad], checks=checks, **fail_kw())["issue"] == "#narrow"
    assert (
        classify([broad, narrow], checks=checks, **fail_kw(case_id="tools.x"))["issue"] == "#broad"
    )


@pytest.mark.parametrize(
    ("observed", "wanted", "expected"),
    [
        ("1.104.1", "1.104.1", True),
        ("1.104.2", "1.104.1", True),
        ("1.110.0", "1.104.1", True),
        ("1.99.9", "1.104.1", False),
        ("BaseHTTP/0.6 Python/3.12.3", "BaseHTTP/0.1", True),
        # A literal prefix selects which number is compared.
        ("BaseHTTP/0.6 Python/3.12.3", "Python/3.13", False),
        ("BaseHTTP/0.6 Python/3.12.3", "Python/3.12", True),
        ("nginx/1.25.3 (Ubuntu)", "nginx/1.26", False),
        ("BaseHTTP/0.6 Python/3.12.3", "Envoy/1.0", False),
        ("vllm-g2107750", "vllm-g2107750", True),
        ("vllm-g5b2a143", "vllm-g2107750", False),
        ("nginx", "1.0", False),
    ],
)
def test_at_or_past_compares_dotted_versions_and_otherwise_equality(observed, wanted, expected):
    assert at_or_past(observed, wanted) is expected


def test_unmatched_entries_are_the_ones_that_labelled_nothing():
    entries = [ENTRY, KnownFailure(check="http_status", issue="#2", cases=("tools.*",))]
    records = [{"known_failure": {"status": "known", "issue": "#1", "check": "finish_reason"}}]
    assert unmatched(entries, records) == [{"check": "http_status", "issue": "#2"}]


# ----------------------------------------------------------------- the profile


def write(tmp_path, text):
    path = tmp_path / "p.yaml"
    path.write_text(textwrap.dedent(text))
    return path


BASE = """
schema: llmdv.profile/1
model: {public_id: m, max_model_len: 4096}
ingresses:
  - id: gw
    kind: litellm
    base_url: http://127.0.0.1:1
    auth: {type: none}
    surfaces: [chat_completions]
"""


def test_the_block_is_parsed_and_normalised(tmp_path):
    block = """
    known_failures:
      - check: error_envelope
        cases: ["discovery.model_retrieve*"]
        ingress: gw
        issue: example-org/serving-stack#216
        fix: LiteLLM 1.104.1
        until: {X-LiteLLM-Version: "1.104.1"}
      - {check: model_echo, issue: "https://github.com/org/repo/issues/5"}
    """
    profile = loader.load(write(tmp_path, BASE + textwrap.dedent(block)))
    first, second = profile.known_failures
    assert first.cases == ("discovery.model_retrieve*",) and first.ingresses == ("gw",)
    assert first.until == {"x-litellm-version": "1.104.1"}
    assert second.cases == () and second.ingresses == () and second.until == {}
    diagnostics = loader.validate(profile)
    assert not [d for d in diagnostics if d.level == "error"]
    # An entry with neither cases nor ingress covers every failure of its check: said, not refused.
    assert [d for d in diagnostics if "known_failures[1]" in d.path and d.level == "warning"]


def test_ingress_takes_one_id_or_a_list(tmp_path):
    text = BASE + "known_failures:\n  - {check: x, issue: '#1', ingress: [gw, gw]}\n"
    assert loader.load(write(tmp_path, text)).known_failures[0].ingresses == ("gw", "gw")


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("- {issue: '#1'}", "check"),
        ("- {check: '*', issue: '#1'}", "literal character"),
        ("- {check: '*?', issue: '#1'}", "literal character"),
        ("- {check: x}", "issue"),
        ("- {check: x, issue: 'GH-12'}", "owner/repo#N"),
        ("- {check: x, issue: '#1', until: [a]}", "until"),
        ("- {check: x, issue: '#1', until: {x-litellm-version: ''}}", "quoted version"),
        ("- {check: x, issue: '#1', until: {x-litellm-version: 1.100}}", "quoted version"),
        ("- {check: x, issue: '#1', fixes: y}", "unknown keys"),
    ],
)
def test_a_malformed_entry_is_refused_with_its_path(tmp_path, entry, message):
    with pytest.raises(loader.ProfileError, match=message):
        loader.load(write(tmp_path, BASE + "known_failures:\n  " + entry + "\n"))


def test_an_undeclared_ingress_is_a_validation_error(tmp_path):
    text = BASE + "known_failures:\n  - {check: x, issue: '#1', ingress: [gw, engine]}\n"
    errors = [
        str(d) for d in loader.validate(loader.load(write(tmp_path, text))) if d.level == "error"
    ]
    assert errors and "engine" in errors[0] and "known_failures[0]" in errors[0]


def test_editing_the_block_or_a_comment_leaves_the_contract_digest_alone(tmp_path):
    plain = loader.contract_digest(write(tmp_path, BASE))
    annotated = loader.contract_digest(
        write(
            tmp_path,
            BASE + "# probe: measured 404\n" + "known_failures:\n  - {check: x, issue: '#1'}\n",
        )
    )
    changed = loader.contract_digest(write(tmp_path, BASE.replace("4096", "8192")))
    assert plain == annotated
    assert plain != changed
    assert plain.startswith("sha256:") and len(plain) == len("sha256:") + 16
    # A mapping with a non-string key must not break the digest: sort_keys cannot order them.
    assert loader.contract_digest(write(tmp_path, BASE + "headers: {200: {gw: ignored}}\n"))


# ----------------------------------------------------------------- a run


def run(tmp_path, fleet, block: str, keyword="test_chat_basic and not sdk"):
    profile = tmp_path / "fake.yaml"
    profile.write_text(profile_yaml(fleet.base_url, fleet.model) + block)
    run_dir = tmp_path / "run"
    result = run_llmdv(
        "verify", "--profile", str(profile), "--local-output", str(run_dir),
        "--pod-logs", "off", "--no-color", "-k", keyword,
    )  # fmt: skip
    records = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    outcome = json.loads((run_dir / "outcome.json").read_text())
    return result, records, outcome, run_dir


def test_a_known_failure_still_fails_and_exits_1_but_says_it_is_known(tmp_path):
    with FakeFleet(abort_finish_reason=True) as fleet:
        result, records, outcome, run_dir = run(tmp_path, fleet, KNOWN_BLOCK)
    assert result.returncode == 1, result.stderr[-2000:]
    (record,) = records
    assert record["outcome"] == "fail"
    assert record["known_failure"]["status"] == "known"
    assert record["known_failure"]["issue"] == ISSUE
    assert record["known_failure"]["check"] == "finish_reason"
    verification = outcome["verification"]
    assert verification["counts"]["fail"] == 1
    assert verification["known"] == {
        "failing": 1,
        "fix_expected": 0,
        "issues": {ISSUE: 1},
        "unmatched": [],
    }
    assert verification["failures"][0]["known_failure"] == {"status": "known", "issue": ISSUE}
    assert "1 of 1 case failed (1 known, fix pending)" in result.stdout
    assert f"known: {ISSUE}; fix: the fake stops aborting" in result.stdout
    assert "known failures" in result.stdout
    summary = (run_dir / "summary.md").read_text()
    assert "fail:known" in summary and "known failures (fix pending" in summary
    verdict = next((run_dir / "failures").rglob("verdict.txt")).read_text()
    assert f"known    {ISSUE} (declared in the profile; fix: the fake stops aborting)" in verdict
    feed = alerts.build(run_dir)
    (entry,) = [r for r in feed["results"] if r["outcome"] == "fail"]
    assert entry["known_failure"] == {
        "status": "known",
        "issue": ISSUE,
        "fix": "the fake stops aborting",
    }


def test_the_same_case_failing_another_check_is_a_plain_failure(tmp_path):
    block = KNOWN_BLOCK.replace("check: finish_reason", "check: choice_index")
    with FakeFleet(abort_finish_reason=True) as fleet:
        result, records, outcome, run_dir = run(tmp_path, fleet, block)
    assert result.returncode == 1
    assert records[0]["known_failure"] is None
    assert outcome["verification"]["known"]["failing"] == 0
    assert "(1 known" not in result.stdout
    # The entry labelled nothing, and the run says so: prune it, or widen the selection.
    assert outcome["verification"]["known"]["unmatched"] == [
        {"check": "choice_index", "issue": ISSUE}
    ]
    assert "matched no failure this run (choice_index)" in result.stdout
    assert "matched no failure" in (run_dir / "summary.md").read_text()


def test_a_known_failure_on_the_awaited_build_is_reported_as_a_fix_that_did_not_land(tmp_path):
    # The fake answers with Python's `server` header, `BaseHTTP/0.6 ...`, which the base
    # adapter records as a build signal. 0.1 is behind it, so the fix was expected.
    block = KNOWN_BLOCK + '    until: {server: "BaseHTTP/0.1"}\n'
    with FakeFleet(abort_finish_reason=True) as fleet:
        result, records, outcome, _ = run(tmp_path, fleet, block)
    assert result.returncode == 1
    known = records[0]["known_failure"]
    assert known["status"] == "fix_expected"
    assert known["observed"]["server"].startswith("BaseHTTP/")
    assert outcome["verification"]["known"] == {
        "failing": 0,
        "fix_expected": 1,
        "issues": {},
        "unmatched": [],
    }
    assert "expected on this build" in result.stdout and "still failing" in result.stdout
    assert "(1 known, fix pending)" not in result.stdout


def test_a_passing_run_is_untouched_by_the_block_and_names_the_idle_entry(tmp_path):
    with FakeFleet() as fleet:
        result, records, outcome, _ = run(tmp_path, fleet, KNOWN_BLOCK)
    assert result.returncode == 0, result.stderr[-2000:]
    assert records[0]["outcome"] == "pass"
    assert records[0]["known_failure"] is None
    assert outcome["verification"]["known"] == {
        "failing": 0,
        "fix_expected": 0,
        "issues": {},
        "unmatched": [{"check": "finish_reason", "issue": ISSUE}],
    }


# ----------------------------------------------------------------- the banner and the comparison


def test_the_headline_counts_known_failures_without_subtracting_them(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(banner, "emit", lambda s="": lines.append(s))
    records = [
        {"outcome": "pass", "case_id": "a"},
        {"outcome": "fail", "case_id": "b", "known_failure": {"status": "known", "issue": "#9"}},
        {"outcome": "fail", "case_id": "c"},
    ]
    banner.finish(
        Path("/nowhere"), records, [], 1.0, known_unmatched=[{"check": "x", "issue": "#7"}]
    )
    headline = next(line for line in lines if "FAILED" in line)
    assert "2 of 3 cases failed (1 known, fix pending)" in headline
    assert any("#9" in line and "×1" in line for line in lines)
    assert any("#7" in line and "matched no failure this run (x)" in line for line in lines)


def rec(case, outcome, known=None, ingress="litellm"):
    out = {"case_id": case, "outcome": outcome, "ingress": ingress, "profile_digest": "d"}
    if known:
        out["known_failure"] = known
    return out


def test_a_known_failure_that_was_already_failing_is_not_a_regression():
    before = [rec("b", "fail"), rec("c", "fail", {"status": "known", "issue": "#1"})]
    after = [
        rec("b", "fail", {"status": "known", "issue": "#1"}),
        rec("c", "fail", {"status": "known", "issue": "#1"}),
    ]
    delta = compare(before, after)
    assert not delta.regressed and delta.still_failing == []
    assert [e["label"] for e in delta.known_failing] == ["b@litellm", "c@litellm"]
    assert delta.current_failing == ["b@litellm", "c@litellm"]
    assert {k[1]: v for k, v in delta.transitions.items()} == {"b": "known", "c": "known"}
    text = render(delta, "p", "r1", "r2")
    assert "2 failing, 2 of them known, no new regressions" in text
    assert "*Known failures, fix pending* (2)" in text
    record = as_record(delta, "r1", "r2")
    assert record["known"]["failing"][0] == {
        "ingress": "litellm",
        "case_id": "b",
        "variant": None,
        "label": "b@litellm",
        "issue": "#1",
    }


def test_a_regression_is_never_known_whatever_the_profile_says():
    before = [rec("a", "pass")]
    after = [rec("a", "fail", {"status": "known", "issue": "#1"})]
    delta = compare(before, after)
    assert delta.regressed and delta.newly_failing == ["a@litellm"]
    assert delta.known_failing == []
    assert [e["label"] for e in delta.regressed_known] == ["a@litellm"]
    assert delta.transitions[("litellm", "a", "")] == "regressed"
    text = render(delta, "p", "r1", "r2")
    assert "1 newly failing" in text
    assert "A regression is never known" in text and "`a@litellm` (#1)" in text
    assert as_record(delta, "r1", "r2")["known"]["regressed"][0]["label"] == "a@litellm"


def test_a_known_failure_that_passes_after_failing_is_the_fix_landing():
    before = [rec("a", "fail", {"status": "known", "issue": "#1"}), rec("b", "fail")]
    after = [rec("a", "pass"), rec("b", "pass")]
    delta = compare(before, after)
    assert delta.newly_passing == ["a@litellm", "b@litellm"]
    # Only the case the previous run had labelled: a recovery from an unlabelled failure is
    # just a recovery, whatever globs the profile carries.
    assert [e["label"] for e in delta.fix_landed] == ["a@litellm"]
    assert "*Fix landed* (1)" in render(delta, "p", "r1", "r2")


def test_a_known_case_that_is_a_plain_failure_now_is_no_longer_covered():
    was = {"status": "known", "issue": "#1", "check": "finish_reason"}
    other_check = compare(
        [rec("a", "fail", was)],
        [{**rec("a", "fail"), "checks": [{"name": "choice_index", "ok": False}]}],
    )
    assert other_check.still_failing == ["a@litellm"]
    assert other_check.known_changed[0]["check"] == "choice_index"
    text = render(other_check, "p", "r1", "r2")
    assert "*No longer covered* (1)" in text
    assert "(was #1 for `finish_reason`; now fails `choice_index`)" in text
    # The same check with no label means the entry was removed or narrowed, not a new cause.
    removed = compare(
        [rec("a", "fail", was)],
        [{**rec("a", "fail"), "checks": [{"name": "finish_reason", "ok": False}]}],
    )
    assert "same check `finish_reason`, no entry covers it now" in render(removed, "p", "r1", "r2")


def test_a_fix_that_did_not_land_stays_a_regression_and_is_named():
    before = [rec("a", "pass")]
    after = [
        rec(
            "a",
            "fail",
            {"status": "fix_expected", "issue": "#1", "observed": {"x-litellm-version": "1.104.1"}},
        )
    ]
    delta = compare(before, after)
    assert delta.regressed and delta.newly_failing == ["a@litellm"]
    assert delta.fix_missing[0]["observed"] == {"x-litellm-version": "1.104.1"}
    text = render(delta, "p", "r1", "r2")
    assert "`a@litellm (#1; saw x-litellm-version=1.104.1)`" in text
    assert "*Fix expected, still failing* (1)" in text


def test_known_failures_are_applied_without_a_window_too():
    delta = compare([], [rec("a", "fail", {"status": "known", "issue": "#1"})])
    assert [e["label"] for e in delta.known_failing] == ["a@litellm"]


def test_known_summary_counts_by_issue_most_first():
    records = [
        rec("a", "fail", {"status": "known", "issue": "#1"}),
        rec("b", "fail", {"status": "known", "issue": "#1"}),
        rec("c", "fail", {"status": "fix_expected", "issue": "#2"}),
        rec("d", "pass"),
        rec("e", "fail"),
        rec("f", "fail", {"status": "known", "issue": "#0"}),
        rec("g", "fail", {"status": "known", "issue": "#0"}),
        rec("h", "fail", {"status": "known", "issue": "#0"}),
    ]
    assert known_summary(records) == {
        "failing": 5,
        "fix_expected": 1,
        "issues": {"#0": 3, "#1": 2},
        "unmatched": [],
    }
