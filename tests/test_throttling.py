"""A 429 from a layer we are rate-limited at is not a fleet failure.

This is what makes a deliberately small quota usable: the request yields instead of competing,
and the suite has to understand the refusal that produces. Reporting it as a failure would send
whoever reads the run looking for a fault that does not exist -- unattended, at whatever hour
contention happens.

The other direction matters just as much. An *undeclared* 429 is a real finding about a layer we
did not expect to throttle, and must stay a failure.
"""

from __future__ import annotations

from tests.fakes import FakeFleet, profile_yaml
from tests.test_suites_against_fake import outcomes, run_suites


def write(tmp_path, fleet, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "fake.yaml"
    path.write_text(profile_yaml(fleet.base_url, fleet.model, **kw))
    return path


def test_a_declared_throttle_skips_rather_than_fails(tmp_path):
    with FakeFleet(throttle=True) as fleet:
        profile = write(tmp_path, fleet, rate_limited=True)
        _, records = run_suites(profile, tmp_path / "run", "-k", "test_chat_basic and not sdk")

    rows = [r for r in records if r.get("case_id") == "openai_chat.basic_non_streaming"]
    assert rows, "no row for the case at all"
    assert rows[0]["outcome"] == "skip", rows[0]
    assert rows[0]["skip_class"] == "throttled", rows[0]


def test_an_undeclared_throttle_is_still_a_failure(tmp_path):
    """A layer we did not expect to throttle is a finding, not a skip."""
    with FakeFleet(throttle=True) as fleet:
        profile = write(tmp_path, fleet, rate_limited=False)
        _, records = run_suites(profile, tmp_path / "run", "-k", "test_chat_basic and not sdk")

    assert outcomes(records).get("openai_chat.basic_non_streaming") == "fail"


def test_the_retry_delay_and_quota_are_recorded(tmp_path):
    """A throttle with no indication of how long or what the quota was is a dead end."""
    with FakeFleet(throttle=True, throttle_signals=True) as fleet:
        profile = write(tmp_path, fleet, rate_limited=True)
        _, records = run_suites(profile, tmp_path / "run", "-k", "test_chat_basic and not sdk")

    rows = [r for r in records if r.get("case_id") == "openai_chat.basic_non_streaming"]
    blob = str(rows[0])
    assert "retry-after" in blob, blob
    assert "x-ratelimit-remaining-requests" in blob, blob
    assert "7" in (rows[0].get("reason") or ""), rows[0].get("reason")


def test_a_throttle_with_no_signals_still_classifies(tmp_path):
    """A gateway that throttles silently is harder to investigate but must not fail the run."""
    with FakeFleet(throttle=True, throttle_signals=False) as fleet:
        profile = write(tmp_path, fleet, rate_limited=True)
        _, records = run_suites(profile, tmp_path / "run", "-k", "test_chat_basic and not sdk")

    rows = [r for r in records if r.get("case_id") == "openai_chat.basic_non_streaming"]
    assert rows[0]["skip_class"] == "throttled", rows[0]
    assert "none returned" in str(rows[0]), rows[0]


def test_declaring_rate_limited_does_not_excuse_other_failures(tmp_path):
    """The declaration covers 429 and nothing else: a 500 is still a 500."""
    with FakeFleet(fall_back_on_unknown_model=True) as fleet:
        profile = write(tmp_path, fleet, rate_limited=True)
        _, records = run_suites(
            profile, tmp_path / "run", "-k", "test_unknown_model_rejected_without_fallback"
        )

    assert outcomes(records).get("discovery.unknown_model_no_fallback") == "fail"


def test_an_unthrottled_fleet_is_unaffected(tmp_path):
    with FakeFleet() as fleet:
        profile = write(tmp_path, fleet, rate_limited=True)
        _, records = run_suites(profile, tmp_path / "run", "-k", "test_chat_basic and not sdk")

    assert outcomes(records).get("openai_chat.basic_non_streaming") == "pass"


# --------------------------------------------------------- every generation path, not only one
#
# Review found that the first version handled only non-streaming JSON: a declared 429 skipped raw
# chat while chat streaming, Messages streaming and the SDK cases all failed, in the same run, for
# the same reason. These assert the gap stays closed.


def test_a_declared_throttle_skips_chat_streaming(tmp_path):
    with FakeFleet(throttle=True) as fleet:
        profile = write(tmp_path, fleet, rate_limited=True)
        _, records = run_suites(profile, tmp_path / "run", "-k", "test_streaming_sse")

    rows = [r for r in records if r.get("case_id") == "openai_chat.streaming_sse"]
    assert rows, "no row for the streaming case"
    assert rows[0]["skip_class"] == "throttled", rows[0]


def test_a_declared_throttle_skips_messages_streaming(tmp_path):
    with FakeFleet(throttle=True) as fleet:
        profile = write(tmp_path, fleet, rate_limited=True)
        _, records = run_suites(profile, tmp_path / "run", "-k", "test_messages_streaming")

    rows = [r for r in records if r.get("case_id", "").startswith("anthropic_messages.streaming")]
    assert rows, "no row for the messages streaming case"
    assert rows[0]["skip_class"] == "throttled", rows[0]


def test_a_declared_throttle_skips_the_sdk_cases(tmp_path):
    """The SDK raises rather than returning an exchange, so it needs its own seam."""
    with FakeFleet(throttle=True) as fleet:
        profile = write(tmp_path, fleet, rate_limited=True)
        _, records = run_suites(profile, tmp_path / "run", "-k", "sdk")

    sdk_rows = [r for r in records if "sdk" in r.get("case_id", "")]
    assert sdk_rows, "no SDK rows at all"
    bad = [r for r in sdk_rows if r["outcome"] == "fail"]
    assert not bad, f"SDK cases still failing under a declared throttle: {bad}"


def test_no_case_fails_anywhere_under_a_declared_throttle(tmp_path):
    """The whole point: a throttled run reports throttling, not a red fleet."""
    with FakeFleet(throttle=True) as fleet:
        profile = write(tmp_path, fleet, rate_limited=True)
        _, records = run_suites(profile, tmp_path / "run")

    failures = [
        (r.get("case_id"), r.get("reason")) for r in records if r["outcome"] in ("fail", "error")
    ]
    assert not failures, f"a declared throttle still produced failures: {failures}"


def test_a_shared_fleet_that_also_throttles_classifies_each_case_correctly(tmp_path):
    """The two declarations are independent and must not mask one another.

    `fleet_policy: shared` and `rate_limited: true` arrived in separate changes that both add a
    skip class. A disruptive case on a shared fleet must still read as excluded_by_policy -- it
    was declined before a request was sent, so it was never throttled -- while a case that does
    run and meets a 429 reads as throttled.
    """
    with FakeFleet(throttle=True) as fleet:
        profile = write(tmp_path, fleet, rate_limited=True, fleet_policy="shared")
        _, records = run_suites(profile, tmp_path / "run")

    classes = {r.get("case_id"): r.get("skip_class") for r in records}
    assert classes.get("unhappy.malformed_json") == "excluded_by_policy", classes
    assert classes.get("openai_chat.basic_non_streaming") == "throttled", classes

    failures = [r.get("case_id") for r in records if r["outcome"] in ("fail", "error")]
    assert not failures, f"shared + throttled still produced failures: {failures}"
