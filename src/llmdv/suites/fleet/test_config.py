"""Configuration invariants, read with `kubectl get` and nothing else.

Each case is one kind of check over rules the profile declares: a setting present or absent,
values that must agree across objects, a field's value, a Service's ready endpoint count, and
a pool whose pods no other object selects. They send no model traffic and change nothing; the
read-only kubectl adapter's verb allowlist is the only way they reach a cluster.

Every case first asserts that its selection found something. An empty read is the most common
way a configuration check passes on a broken fleet: a renamed workload, a changed label, a
namespace that moved.
"""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...evidence import journal
from ...fleet import config as cfg
from ...kube import KubeError
from ...profile.fleet import ObjectRef
from ...profile.schema import SkipClass
from .helpers import absent_error, declared_rule, gate, read, require_kubectl

# The label EndpointSlices carry for the Service they belong to.
SERVICE_NAME_LABEL = "kubernetes.io/service-name"


def _rule(profile, case, fleet_kube):
    rule = declared_rule(profile, case.label)
    journal.record("rule", rule.id)
    gate(profile, rule)
    require_kubectl(fleet_kube)
    return rule


def _env_configmaps(fleet_kube, objects, view, ref, profile) -> dict[str, dict]:
    """Data of the ConfigMaps the containers take variables from, so their values are judged.

    A missing optional ConfigMap is left as <from>; a missing required one fails, and an
    unreadable one skips because its effective variables cannot be judged from this vantage.
    """
    if view != "containers":
        return {}
    namespace = ref.namespace or profile.environment.namespace
    found: dict[str, dict] = {}
    requirements: dict[str, bool] = {}
    for obj in objects:
        for name, required in cfg.configmap_requirements(obj).items():
            requirements[name] = requirements.get(name, False) or required
    for name, required in sorted(requirements.items()):
        try:
            found[name] = (
                fleet_kube.get_objects("configmaps", name, namespace=namespace)[0].get("data") or {}
            )
        except KubeError as exc:
            journal.record(f"configmap {name}", str(exc)[:200])
            if absent_error(exc) and required:
                fail(f"configmap {name}", str(exc)[:300], "the required ConfigMap exists")
            if not absent_error(exc):
                pytest.skip(
                    f"[{SkipClass.REQUIRES_INSTRUMENTATION.value}] could not read ConfigMap "
                    f"{name}: {str(exc)[:200]}"
                )
    if found:
        journal.record("env from configmaps", sorted(found))
    return found


@pytest.mark.case_id("config.settings")
def test_settings(profile, config_rule, fleet_kube):
    """Declared patterns are present, and forbidden ones absent, on every selected unit."""
    rule = _rule(profile, config_rule, fleet_kube)
    objects = read(fleet_kube, rule.objects, profile)
    configmaps = _env_configmaps(fleet_kube, objects, rule.view, rule.objects, profile)
    pieces = [
        piece for obj in objects for piece in cfg.units(obj, rule.view, rule.containers, configmaps)
    ]
    labels = [label for label, _ in pieces]
    journal.record(f"{rule.view} read", labels, "at least one", ok=bool(pieces))
    if not pieces:
        fail(f"{rule.view} read", "none", f"at least one {rule.view} unit in the selection")
    bad = cfg.settings_violations(pieces, rule)
    journal.record(
        "settings",
        bad or f"{len(pieces)} compliant",
        f"require {list(rule.require)}, forbid {list(rule.forbid)}",
        ok=not bad,
    )
    if bad:
        fail("settings", bad, "every unit has each required pattern and no forbidden one")


def _capability_values(profile, path: str) -> set[str]:
    """Declared values of a dotted capability path, across every ingress that declares it."""
    found = set()
    for ingress in profile.ingresses:
        node = ingress.capabilities
        for part in path.split("."):
            node = getattr(node, part, None)
            if node is None:
                break
        if node is not None:
            found.add(str(node))
    return found


@pytest.mark.case_id("config.values_agree")
def test_values_agree(profile, config_rule, fleet_kube):
    """Values extracted from several objects are one value, equal to any declared expectation."""
    rule = _rule(profile, config_rule, fleet_kube)
    seen: dict[str, str] = {}
    for index, source in enumerate(rule.sources):
        objects = read(fleet_kube, source.objects, profile)
        configmaps = _env_configmaps(fleet_kube, objects, source.view, source.objects, profile)
        values, missing = cfg.extract(objects, source, configmaps)
        journal.record(
            f"source {index} values",
            values,
            "a value from every unit",
            ok=bool(values) and not missing,
        )
        if missing or not values:
            fail(
                f"source {index} values",
                {"missing": missing, "found": values},
                f"{source.pattern!r} matches every unit of {source.objects.describe()}",
            )
        seen.update({f"{index}:{label}": value for label, value in values.items()})

    expected: set[str] = set()
    if rule.expect is not None:
        expected.add(rule.expect)
    if rule.expect_capability:
        declared = _capability_values(profile, rule.expect_capability)
        journal.record(rule.expect_capability, sorted(declared) or "not declared")
        expected |= declared
    distinct = set(seen.values()) | expected
    journal.record(
        "values",
        seen,
        f"one value{f' equal to {sorted(expected)}' if expected else ''}",
        ok=len(distinct) == 1,
    )
    if len(distinct) != 1:
        fail("values", {"found": seen, "declared": sorted(expected)}, "a single agreed value")


@pytest.mark.case_id("config.field_values")
def test_field_values(profile, config_rule, fleet_kube):
    """A field holds the declared value, or a duration of at least the declared seconds."""
    rule = _rule(profile, config_rule, fleet_kube)
    objects = read(fleet_kube, rule.objects, profile)
    observed, bad = {}, {}
    for obj in objects:
        name = cfg.object_name(obj)
        value = cfg.dig(obj, rule.path)
        observed[name] = value
        if rule.at_least_seconds is not None:
            seconds = cfg.duration_seconds(value)
            ok = seconds is not None and seconds >= rule.at_least_seconds
        else:
            ok = value == rule.equals or (value is not None and str(value) == str(rule.equals))
        if not ok:
            bad[name] = value
    expected = (
        f">= {rule.at_least_seconds:g} s"
        if rule.at_least_seconds is not None
        else repr(rule.equals)
    )
    journal.record(".".join(rule.path), observed, expected, ok=not bad)
    if bad:
        fail(".".join(rule.path), bad, f"{expected} on every selected object")


@pytest.mark.case_id("config.ready_endpoints")
def test_ready_endpoints(profile, config_rule, fleet_kube):
    """A Service has exactly the declared number of ready endpoints, such as one active router.

    Read from the Service's EndpointSlices, the current API, and from its Endpoints object
    where a cluster or a reader has no slices.
    """
    rule = _rule(profile, config_rule, fleet_kube)
    namespace = rule.objects.namespace or profile.environment.namespace
    try:
        slices = fleet_kube.get_objects(
            "endpointslices", selector=f"{SERVICE_NAME_LABEL}={rule.service}", namespace=namespace
        )
    except KubeError as exc:
        journal.record("endpointslices", str(exc)[:200])
        slices = []
    if slices:
        ready = cfg.ready_in_slices(slices)
        journal.record("read endpointslices", [cfg.object_name(item) for item in slices])
    else:
        # A cluster without EndpointSlices, or a reader not allowed them: the Endpoints object.
        ready = cfg.ready_addresses(read(fleet_kube, rule.objects, profile)[0])
    journal.record("ready_endpoints", ready, f"exactly {rule.count}", ok=len(ready) == rule.count)
    if len(ready) != rule.count:
        fail("ready_endpoints", ready, f"exactly {rule.count} ready endpoint(s) on {rule.service}")


@pytest.mark.case_id("config.selector_exclusive")
def test_selector_exclusive(profile, config_rule, fleet_kube):
    """No other object of the same kind selects any pod the declared object selects."""
    rule = _rule(profile, config_rule, fleet_kube)
    ref = rule.objects
    owner = read(fleet_kube, ref, profile)[0]
    selector = cfg.dig(owner, rule.selector_path)
    if (
        not isinstance(selector, dict)
        or not selector
        or not all(isinstance(value, str) for value in selector.values())
    ):
        # A path that stops one level short, at a selector holding `matchLabels`, lands here.
        fail("owner selector", selector, "a non-empty label map at the declared path")
    pods = read(
        fleet_kube,
        ObjectRef(kind="pods", selector=cfg.selector_text(selector), namespace=ref.namespace),
        profile,
    )
    labels = [(pod.get("metadata") or {}).get("labels") or {} for pod in pods]
    others = read(
        fleet_kube, ObjectRef(kind=ref.kind, namespace=ref.namespace), profile, allow_empty=True
    )
    overlapping = sorted(
        cfg.object_name(other)
        for other in others
        if cfg.object_name(other) != ref.name
        and any(cfg.selects(cfg.dig(other, rule.selector_path), item) for item in labels)
    )
    journal.record(
        "also_selecting", overlapping, f"no other {ref.kind} on these pods", ok=not overlapping
    )
    if overlapping:
        fail("also_selecting", overlapping, f"only {ref.name} selects its {len(pods)} pod(s)")
