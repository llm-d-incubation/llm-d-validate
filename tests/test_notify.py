"""Slack notifications: what the message says, and how delivery behaves when Slack pushes back."""

from __future__ import annotations

import json
import re

import pytest

from llmdv import notify
from tests.fakes import FakeSlack, run_llmdv


def outcome(**overrides):
    base = {
        "run_id": "2026-10-01T02-00-00Z-ab12",
        "llmdv_version": "0.1.0",
        "git_sha": "0123456789abcdef",
        "profile": {"name": "fleet-a"},
        "labels": {"trigger": "rollout", "rollout": "r-42"},
        "verification": {
            "exit_code": 0,
            "class": "ok",
            "counts": {"pass": 20, "skip": 2},
            "skips": {"undeclared": 2},
            "failures": [],
        },
        "preflight_problems": [],
        "publication": {"status": "published", "location": "s3://bucket/p/fleet-a/run/"},
    }
    base.update(overrides)
    return base


def test_messages_are_plain_text():
    """No emoji and no Slack formatting: a status word and plain ASCII lines."""
    texts = [notify.message(None, exit_code=137)]
    for klass in notify.STATUS:
        verification = {"exit_code": 0, "class": klass, "counts": {}, "skips": {}, "failures": []}
        texts.append(notify.message(outcome(verification=verification)))
    for text in texts:
        assert text.isascii(), text
        assert not re.search(r":[a-z_]+:", text), text  # emoji shortcodes such as :x:
        assert "*" not in text and "`" not in text, text


def test_a_passing_run_names_its_trigger_counts_skips_and_where_results_are():
    text = notify.message(outcome())
    assert text.splitlines()[0] == "llmdv fleet-a: PASSED, all selected cases passed (exit 0)"
    assert "trigger=rollout, rollout=r-42" in text
    # Skips are spelled out, so a run whose coverage quietly shrank does not read as clean.
    assert "skipped 2 (2 undeclared)" in text
    assert "s3://bucket/p/fleet-a/run/" in text
    assert "(0123456)" in text


def test_failed_tests_and_failed_publication_are_both_reported():
    text = notify.message(
        outcome(
            verification={
                "exit_code": 1,
                "class": "fleet",
                "counts": {"pass": 1, "fail": 1},
                "skips": {},
                "failures": [
                    {"case_id": "c.one", "variant": "chat_completions", "ingress": "router",
                     "reason": "AssertionError: x < y\nsecond line of the traceback"},
                ],
            },
            publication={"status": "failed", "error": "AccessDenied"},
        )
    )  # fmt: skip
    assert text.startswith("llmdv fleet-a: FAILED, test failures")
    assert "- c.one[chat_completions] at router: AssertionError: x &lt; y\n" in text
    assert "second line of the traceback" not in text
    assert "results NOT published: AccessDenied" in text


def test_notifying_from_a_published_copy_does_not_claim_it_was_unpublished():
    """The copy inside a bundle records its destination, not how its own upload went."""
    text = notify.message(outcome(publication={"destination": "s3://bucket/p"}))
    assert "sent to s3://bucket/p" in text
    assert "not published" not in text


def test_a_run_that_left_no_outcome_reports_the_exit_code_it_had():
    text = notify.message(None, exit_code=4)
    assert "llmdv failed internally" in text and "exit 4" in text


def test_a_rate_limit_is_retried_after_the_delay_slack_asks_for():
    waits = []
    with FakeSlack(script=[(429, {"Retry-After": "7"})]) as slack:
        notify.post(slack.base_url, "hello", sleep=waits.append)
    assert waits == [7.0]
    assert [body["text"] for body in slack.received] == ["hello", "hello"]


def test_a_retry_delay_longer_than_the_run_allows_stops_delivery_explicitly():
    """Retrying sooner than Slack asked only spends the remaining attempts uselessly."""
    with FakeSlack(script=[(429, {"Retry-After": "120"})]) as slack:
        with pytest.raises(notify.NotifyError, match="retry after 120s"):
            notify.post(slack.base_url, "hello", sleep=lambda _: pytest.fail("must not wait"))
    assert len(slack.received) == 1


def test_a_rejected_message_is_not_retried():
    with FakeSlack(script=[(404, {})]) as slack:
        with pytest.raises(notify.NotifyError, match="refused"):
            notify.post(slack.base_url, "hello", sleep=lambda _: None)
    assert len(slack.received) == 1


def test_server_errors_are_retried_a_bounded_number_of_times():
    with FakeSlack(script=[(500, {})] * notify.MAX_ATTEMPTS) as slack:
        with pytest.raises(notify.NotifyError, match="gave up"):
            notify.post(slack.base_url, "hello", sleep=lambda _: None)
    assert len(slack.received) == notify.MAX_ATTEMPTS


def test_the_cli_sends_the_outcome_and_never_logs_the_webhook_url(tmp_path):
    (tmp_path / "outcome.json").write_text(json.dumps(outcome()))
    with FakeSlack() as slack:
        url = f"{slack.base_url}/services/T000/B000/llmdv-secret-token"
        result = run_llmdv("notify", str(tmp_path), "-vv", env={"LLMDV_NOTIFY_WEBHOOK": url})

    assert result.returncode == 0, result.stderr
    assert "fleet-a" in slack.received[0]["text"]
    assert "llmdv-secret-token" not in result.stderr + result.stdout


def test_the_cli_refuses_to_run_without_a_webhook(tmp_path):
    result = run_llmdv("notify", str(tmp_path), "--exit-code", "1")
    assert result.returncode == 2
    assert "LLMDV_NOTIFY_WEBHOOK" in result.stderr


def test_every_exit_class_has_a_status():
    """The exit-code classes are defined once, in exit_codes; notify must cover each of them."""
    from llmdv.cli.exit_codes import CLASS, class_of

    assert set(notify.STATUS) == set(CLASS.values()) | {class_of(137)}


def test_a_fallback_upload_names_the_destination_that_was_unavailable():
    text = notify.message(
        outcome(
            publication={
                "status": "published",
                "location": "/results/fleet-a/run",
                "primary": {"destination": "s3://bucket/p", "error": "AccessDenied"},
            }
        )
    )
    assert "results: /results/fleet-a/run" in text
    assert "saved to the fallback: s3://bucket/p was unavailable (AccessDenied)" in text
