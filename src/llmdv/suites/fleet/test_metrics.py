"""Fleet invariants read from a metrics source, with GET /api/v1/query only.

Each case is one kind of check over rules the profile declares: a value within bounds, two
values that must agree, values that must stay close across series, and a request header that
must land on a declared label. Metric and label names all come from the profile.

Every case first asserts that its query returned series. An empty result is the commonest way a
metrics check passes on a broken fleet: a renamed metric, a counter the build does not export,
or a selector that matches nothing. Only a rule that declares `absent_is_zero` reads an empty
result as zero, for counters that appear once incremented.
"""

from __future__ import annotations

import math
import time
import uuid

import pytest

from ... import surfaces
from ...assertions import assert_not_throttled, fail
from ...evidence import journal
from ...profile.fleet import NONCE
from ...profile.schema import SkipClass
from .helpers import declared_metric, gate, query, require_metrics


def _rule(profile, case, fleet_metrics):
    rule = declared_metric(profile, case.label)
    journal.record("rule", rule.id)
    gate(profile, rule)
    return rule, require_metrics(fleet_metrics)


def _present(samples, what: str, absent_is_zero: bool = False) -> list[float]:
    values = [sample.value for sample in samples]
    if not values and absent_is_zero:
        journal.record(f"{what} series", "absent, read as zero by declaration")
        return [0.0]
    journal.record(f"{what} series", len(values), "at least one", ok=bool(values))
    if not values:
        fail(f"{what} series", "none", "at least one series; an empty result is not healthy")
    bad = [value for value in values if math.isnan(value) or math.isinf(value)]
    if bad:
        fail(f"{what} values", values, "finite numbers")
    return values


def _one(samples, what: str) -> float:
    values = _present(samples, what)
    if len(values) != 1:
        fail(f"{what} series", len(values), "exactly one series; aggregate it in the query")
    return values[0]


@pytest.mark.case_id("metrics.bounds")
def test_bounds(profile, metric_rule, fleet_metrics):
    """Every series of a declared query is within the declared minimum and maximum."""
    rule, reader = _rule(profile, metric_rule, fleet_metrics)
    values = _present(query(reader, rule.query, "query"), "query", rule.absent_is_zero)
    outside = [
        value
        for value in values
        if (rule.min is not None and value < rule.min)
        or (rule.max is not None and value > rule.max)
    ]
    expected = f"within [{rule.min if rule.min is not None else '-inf'}, " + (
        f"{rule.max if rule.max is not None else 'inf'}]"
    )
    journal.record("bounds", values, expected, ok=not outside)
    if outside:
        fail("bounds", outside, expected)


@pytest.mark.case_id("metrics.agree")
def test_agree(profile, metric_rule, fleet_metrics):
    """Two declared values agree within a tolerance, or the left is at least the right."""
    rule, reader = _rule(profile, metric_rule, fleet_metrics)
    left = _one(query(reader, rule.left, "left"), "left")
    right = _one(query(reader, rule.right, "right"), "right")
    if rule.absolute_tolerance is not None:
        slack = rule.absolute_tolerance
        expected = f"{rule.relation} within {slack:g}"
    else:
        slack = rule.relative_tolerance * max(abs(left), abs(right))
        expected = f"{rule.relation} within {rule.relative_tolerance:.2%}"
    if rule.relation == "at_least":
        ok = left >= right - slack
    else:
        ok = abs(left - right) <= slack
    journal.record("agree", {"left": left, "right": right}, expected, ok=ok)
    if not ok:
        fail("agree", {"left": left, "right": right}, expected)


@pytest.mark.case_id("metrics.spread")
def test_spread(profile, metric_rule, fleet_metrics):
    """Across series, the smallest value is at least the declared ratio of the largest."""
    rule, reader = _rule(profile, metric_rule, fleet_metrics)
    samples = query(reader, rule.query, "query")
    if rule.label:
        raw = [sample.labels.get(rule.label) for sample in samples]
        try:
            values = [float(item) for item in raw if item is not None]
        except ValueError:
            fail(f"label {rule.label}", raw, "numbers")
        journal.record(
            f"label {rule.label}", values, "a number on every series", ok=len(values) == len(raw)
        )
        if len(values) != len(raw):
            fail(f"label {rule.label}", raw, "present on every series")
    else:
        values = [sample.value for sample in samples]
    bad = [value for value in values if not math.isfinite(value)]
    if bad:
        fail("spread values", values, "finite numbers")
    journal.record(
        "series", len(values), f"at least {rule.min_series}", ok=len(values) >= rule.min_series
    )
    if len(values) < rule.min_series:
        fail("series", len(values), f"at least {rule.min_series}; an empty result is not healthy")
    low, high = min(values), max(values)
    ok = high > 0 and low >= rule.min_ratio * high
    journal.record("spread", {"min": low, "max": high}, f"min >= {rule.min_ratio:g} x max", ok=ok)
    if not ok:
        fail(
            "spread",
            {"min": low, "max": high, "values": sorted(values)},
            f"min >= {rule.min_ratio:g} x max",
        )


# The first of these an ingress exposes carries the probe request. Any generation surface
# reaches the router's request counters the same way.
_PROBE_SURFACES = ("chat_completions", "messages", "responses", "completions")


def _probe_surface(ingress):
    for name in _PROBE_SURFACES:
        if ingress.exposes(name) and name in surfaces.implemented():
            return surfaces.get(name)
    pytest.skip(
        f"[{SkipClass.UNDECLARED.value}] ingress {ingress.id!r} exposes no generation surface "
        "to carry the probe request"
    )


def _matching(samples, label: str, expect: str) -> float:
    matching = [sample.value for sample in samples if sample.labels.get(label) == expect]
    bad = [value for value in matching if not math.isfinite(value)]
    if bad:
        fail(f"{label}={expect} values", matching, "finite numbers")
    return sum(matching)


@pytest.mark.case_id("metrics.header_label")
def test_header_label(profile, metric_rule, fleet_metrics, client_factory):
    """A request with declared headers is counted under the declared label value.

    Sends one small request through the rule's ingress. With `{nonce}` in the query, the
    request's own series must appear and carry only the expected label value. Without it, the
    series for the expected value must grow by at least one, which is how traffic that a layer
    tags with a fixed identity is followed.
    """
    rule, reader = _rule(profile, metric_rule, fleet_metrics)
    target = profile.ingress(rule.ingress)
    surface = _probe_surface(target)
    nonce = f"llmdv-{uuid.uuid4().hex[:12]}"
    headers = {name: value.replace(NONCE, nonce) for name, value in rule.headers}
    expr = rule.query.replace(NONCE, nonce)
    own_series = NONCE in rule.query
    before = (
        0.0 if own_series else _matching(query(reader, expr, "before"), rule.label, rule.expect)
    )

    http = client_factory(target.id)
    exchange = http.post(
        surface.path,
        json_body=surface.minimal_body(target.model.public_id),
        headers={**surface.extra_headers(), **headers},
    )
    assert_not_throttled(exchange, target)
    ok = exchange.status is not None and 200 <= exchange.status < 300
    journal.record("http_status", exchange.status, "2xx", ok=ok)
    if not ok:
        fail("http_status", exchange.status, "a served probe request")

    deadline = time.monotonic() + rule.timeout_s
    while True:
        samples = query(reader, expr, "after")
        if own_series and samples:
            break
        if not own_series and _matching(samples, rule.label, rule.expect) >= before + 1:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(rule.interval_s)

    if own_series:
        seen = sorted({sample.labels.get(rule.label, "") for sample in samples})
        ok = seen == [rule.expect]
        journal.record(rule.label, seen or "no series", f"[{rule.expect!r}]", ok=ok)
        if not ok:
            fail(rule.label, seen or f"no series in {rule.timeout_s:g} s", f"only {rule.expect!r}")
        return
    grown = _matching(samples, rule.label, rule.expect) - before
    journal.record(f"{rule.label}={rule.expect} grew by", grown, "at least 1", ok=grown >= 1)
    if grown < 1:
        fail(f"{rule.label}={rule.expect} grew by", grown, f"at least 1 in {rule.timeout_s:g} s")
