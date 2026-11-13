"""Collection-time parametrisation and cluster access for the fleet suite.

Each fleet case is one kind of check, and each rule a profile declares for it is one variant,
so declaring a rule creates its case with no code change. A profile with no rule for a check
keeps one row for it, skipped as `undeclared`, so the matrix shows the gap.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple

import pytest

from ...fleet.prometheus import PrometheusReader, token_for
from ...kube import KubeContext
from ...profile.schema import Profile, SkipClass


class FleetCase(NamedTuple):
    """One declared rule. `label` is the variant; `ingress_id` the column it is reported in."""

    label: str
    ingress_id: str | None
    check: str


def _check_of(metafunc: pytest.Metafunc) -> str:
    marker = metafunc.definition.get_closest_marker("case_id")
    return marker.args[0].split(".", 1)[1] if marker and marker.args else ""


def _selected(config: pytest.Config, profile: Profile) -> set[str]:
    return set(config.getoption("--llmdv-ingress") or profile.ingress_ids)


def innermost(config: pytest.Config, profile: Profile) -> str:
    """The selected ingress closest to the engines, where a fleet-wide row is reported.

    A fleet invariant holds for every layer in front of it, so its row goes in one column:
    a row with no ingress is missing from every per-layer tally.
    """
    selected = _selected(config, profile)
    return [item for item in profile.chain() if item in selected][-1]


def _undeclared(check: str, what: str, ingress_id: str) -> list:
    reason = f"[{SkipClass.UNDECLARED.value}] the profile declares no {what} rule for {check}"
    placeholder = FleetCase("undeclared", ingress_id, check)
    return [pytest.param(placeholder, marks=pytest.mark.skip(reason=reason), id="undeclared")]


# Fixture name -> (where the rules are declared, how to list one check's rules).
_RULE_FIXTURES = {
    "config_rule": ("fleet.config", lambda fleet, check: fleet.config_rules(check)),
    "metric_rule": ("fleet.metrics", lambda fleet, check: fleet.metric_rules(check)),
}


class LogCase(NamedTuple):
    """One declared log source with forbidden signatures: its ingress and position."""

    label: str
    ingress_id: str
    index: int


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    for fixture, (what, listed) in _RULE_FIXTURES.items():
        if fixture in metafunc.fixturenames:
            _parametrise_rules(metafunc, fixture, what, listed)
    if "log_source" in metafunc.fixturenames:
        _parametrise_log_sources(metafunc)


def _parametrise_log_sources(metafunc: pytest.Metafunc) -> None:
    """One variant per declared source that forbids something, under its own ingress."""
    profile: Profile | None = getattr(metafunc.config, "_llmdv_profile", None)
    if profile is None:
        metafunc.parametrize("log_source", [], ids=[])
        return
    selected = _selected(metafunc.config, profile)
    cases = [
        LogCase(
            f"{source.selector or source.target}"
            + (f"/{source.container}" if source.container else ""),
            ingress.id,
            index,
        )
        for ingress in profile.ingresses
        if ingress.id in selected
        for index, source in enumerate(ingress.logs)
        if source.forbid
    ]
    if not cases:
        reason = (
            f"[{SkipClass.UNDECLARED.value}] no selected ingress declares a log source "
            "with forbidden signatures"
        )
        placeholder = LogCase("undeclared", innermost(metafunc.config, profile), -1)
        metafunc.parametrize(
            "log_source",
            [pytest.param(placeholder, marks=pytest.mark.skip(reason=reason), id="undeclared")],
        )
        return
    metafunc.parametrize(
        "log_source", cases, ids=[f"{case.ingress_id}:{case.label}" for case in cases]
    )


def pytest_collection_finish(session: pytest.Session) -> None:
    """Give direct pytest runs a log window; llmdv verify passes its earlier run start."""
    if not session.config.getoption("--llmdv-started-at"):
        session.config._llmdv_log_window = datetime.now(timezone.utc).strftime(  # type: ignore[attr-defined]
            "%Y-%m-%dT%H:%M:%SZ"
        )


def _parametrise_rules(metafunc: pytest.Metafunc, fixture: str, what: str, listed) -> None:
    profile: Profile | None = getattr(metafunc.config, "_llmdv_profile", None)
    if profile is None:
        metafunc.parametrize(fixture, [], ids=[])
        return
    check = _check_of(metafunc)
    selected = _selected(metafunc.config, profile)
    rules = [
        rule
        for rule in listed(profile.fleet, check)
        if rule.ingress is None or rule.ingress in selected
    ]
    where = innermost(metafunc.config, profile)
    if not rules:
        metafunc.parametrize(fixture, _undeclared(check, what, where))
        return
    metafunc.parametrize(
        fixture,
        [FleetCase(rule.id, rule.ingress or where, check) for rule in rules],
        ids=[rule.id for rule in rules],
    )


@pytest.fixture(scope="session")
def fleet_kube(request: pytest.FixtureRequest, profile: Profile) -> KubeContext:
    """Read-only cluster access, from the same options and environment as preflight uses."""
    kubeconfig = request.config.getoption("--llmdv-kubeconfig")
    options = {
        "kubeconfig": kubeconfig or profile.environment.kubeconfig,
        "context": request.config.getoption("--llmdv-context") or profile.environment.context,
        "namespace": profile.environment.namespace,
    }
    cli = request.config.getoption("--llmdv-kube-cli")
    if cli is not None:
        options["cli"] = cli
    return KubeContext(**options)


@pytest.fixture(scope="session")
def fleet_metrics(profile: Profile) -> PrometheusReader | None:
    """The declared metrics source, or None when the profile declares none."""
    source = profile.environment.metrics
    if source is None:
        return None
    return PrometheusReader(source, token_for(source))
