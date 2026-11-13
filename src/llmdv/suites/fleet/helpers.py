"""Reads shared by the fleet cases: one rule from the profile, and objects from the cluster."""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...evidence import journal
from ...fleet.config import object_name
from ...fleet.prometheus import MetricsError, PrometheusReader, Sample
from ...kube import KubeContext, KubeError
from ...profile.fleet import ConfigRule, MetricRule, ObjectRef
from ...profile.schema import Profile, SkipClass

# kubectl's wording for a declared object or kind that does not exist. Anything else that fails
# a read (forbidden, unreachable, timed out) says nothing about the fleet.
_ABSENT = ("notfound", "not found", "doesn't have a resource type")


def absent_error(exc: KubeError) -> bool:
    return any(marker in str(exc).lower() for marker in _ABSENT)


_NO_ACCESS = (
    "forbidden",
    "unauthorized",
    "connection refused",
    "unable to connect",
    "no route to host",
    "the server has asked for the client to provide credentials",
)


def instrumentation_error(error: str | KubeError) -> bool:
    """A read failed because this vantage cannot access the cluster, not because of its input."""
    text = str(error).lower()
    return any(marker in text for marker in _NO_ACCESS)


def declared_rule(profile: Profile, rule_id: str) -> ConfigRule:
    return next(rule for rule in profile.fleet.config if rule.id == rule_id)


def require_kubectl(kube: KubeContext) -> None:
    if not kube.available:
        pytest.skip(
            f"[{SkipClass.REQUIRES_INSTRUMENTATION.value}] {kube.unavailable_reason}, so the "
            "declared configuration cannot be read from here"
        )


def gate(profile: Profile, rule: ConfigRule | MetricRule) -> None:
    """Skip a rule that applies only where an ingress declares a capability none declares."""
    if rule.when and not any(
        getattr(ingress.capabilities, rule.when, None) for ingress in profile.ingresses
    ):
        pytest.skip(
            f"[{SkipClass.UNDECLARED.value}] rule {rule.id!r} applies where an ingress "
            f"declares {rule.when!r}, and none does"
        )


def read(kube: KubeContext, ref: ObjectRef, profile: Profile, allow_empty: bool = False):
    """Objects for a reference. An empty selection fails: it never reads as a healthy fleet."""
    namespace = ref.namespace or profile.environment.namespace
    what = f"read {ref.describe()}"
    try:
        objects = kube.get_objects(ref.kind, ref.name, ref.selector, namespace)
    except KubeError as exc:
        text = str(exc)
        if absent_error(exc):
            fail(what, text[:300], "the declared object exists")
        pytest.skip(f"[{SkipClass.REQUIRES_INSTRUMENTATION.value}] could not {what}: {text[:200]}")
    names = [object_name(item) for item in objects]
    journal.record(what, names, "at least one object", ok=bool(names) or allow_empty)
    if not objects and not allow_empty:
        fail(what, "no objects", "at least one object; an empty selection is not a healthy fleet")
    return objects


def declared_metric(profile: Profile, rule_id: str) -> MetricRule:
    return next(rule for rule in profile.fleet.metrics if rule.id == rule_id)


def require_metrics(reader: PrometheusReader | None) -> PrometheusReader:
    if reader is None:
        pytest.skip(
            f"[{SkipClass.UNDECLARED.value}] the profile declares no environment.metrics source"
        )
    return reader


def query(reader: PrometheusReader, expr: str, what: str) -> list[Sample]:
    """Samples for one expression, journaled. A rejected query fails; an unreachable one skips."""
    try:
        samples = reader.query(expr)
    except MetricsError as exc:
        if exc.kind == "rejected":
            fail(what, str(exc), f"the metrics source accepts {expr!r}")
        pytest.skip(
            f"[{SkipClass.REQUIRES_INSTRUMENTATION.value}] metrics source "
            f"{reader.describe()} did not answer: {exc}"
        )
    journal.record(
        what,
        {"query": expr, "series": [{**s.labels, "value": s.value} for s in samples[:20]]},
    )
    return samples
