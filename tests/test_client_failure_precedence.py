"""Session throttling cannot hide an independently failed generation."""

from collections import Counter
from types import SimpleNamespace

import pytest

from llmdv.evidence import journal
from llmdv.http.client import Exchange
from llmdv.suites.real_clients.helpers import check_upstream


@pytest.fixture
def checks():
    active = journal.activate("session")
    yield active.checks
    journal.deactivate()


def relay(statuses, *, rejected=(), transport_error=None):
    exchanges = [
        Exchange(
            "POST",
            "http://example.invalid/v1/messages",
            status=status,
            response_headers={"Retry-After": "12"} if status == 429 else {},
        )
        for status in statuses
    ]
    if transport_error is not None:
        exchanges[transport_error].transport_error = "connection closed"
    return SimpleNamespace(generations=[(e, None) for e in exchanges], rejected=rejected)


@pytest.mark.parametrize("failure", [None, 401, 404, 500, 502])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("rate_limited", [False, True])
def test_independent_failures_take_precedence_in_either_order(
    checks, failure, reverse, rate_limited
):
    statuses = [failure, 429, 200]
    if reverse:
        statuses.reverse()
    captured = relay(statuses, rejected=["retry refused"])
    ingress = SimpleNamespace(id="example", rate_limited=rate_limited)

    with pytest.raises(AssertionError, match="client_upstream_statuses"):
        check_upstream(captured, ingress, "client_upstream_statuses")

    (check,) = checks
    assert not check.ok
    assert check.observed == str(
        {"statuses": dict(Counter(statuses)), "relay_refused": ["retry refused"]}
    )


@pytest.mark.parametrize("status", [200, 429])
def test_transport_failure_is_not_mistaken_for_success_or_throttling(checks, status):
    captured = relay([status, 429], transport_error=0)
    ingress = SimpleNamespace(id="example", rate_limited=True)
    with pytest.raises(AssertionError, match="transport_error"):
        check_upstream(captured, ingress, "upstream")
    assert checks[0].observed == str({"statuses": {"transport_error": 1, 429: 1}})


@pytest.mark.parametrize("statuses", [[429], [200, 429], [429, 200]])
def test_declared_throttle_skips_and_retains_session_and_quota_evidence(checks, statuses):
    ingress = SimpleNamespace(id="example", rate_limited=True)
    with pytest.raises(pytest.skip.Exception, match=r"\[throttled\]"):
        check_upstream(relay(statuses), ingress, "upstream")
    assert checks[0].observed == dict(Counter(statuses))
    assert all(check.ok for check in checks)
    assert next(c for c in checks if c.name == "throttle_signals").observed == {"retry-after": "12"}


def test_undeclared_throttle_remains_a_failure(checks):
    ingress = SimpleNamespace(id="example", rate_limited=False)
    with pytest.raises(AssertionError, match="does not declare it rate limited"):
        check_upstream(relay([429]), ingress, "upstream")
    assert any(c.name == "throttled_undeclared" and not c.ok for c in checks)


def test_since_excludes_earlier_failures_and_healthy_sessions_pass(checks):
    ingress = SimpleNamespace(id="example", rate_limited=True)
    check_upstream(relay([500, 429, 200, 200]), ingress, "upstream", since=2)
    (check,) = checks
    assert check.observed == {200: 2}
    assert check.ok
