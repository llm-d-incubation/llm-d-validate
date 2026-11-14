"""`-m` must select what the operator meant, or say it could not.

Suite package names look like markers. Before this they were not, so `-m discovery` selected
nothing, pytest reported `72 deselected`, and the run completed and published as PASSED over a
smaller set than anybody asked for. A narrower green run is worse than an error, which is the
same reasoning that makes `banner.finish` refuse to print `PASSED | 0 cases`.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from llmdv.plugin import (
    _ACTIVE,
    _check_capability_names,
    _suite_of,
    known_markers,
    register_artifact,
    register_client,
    suite_markers,
)

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "examples" / "example-two-ingress.yaml"
SUITES = Path(__file__).resolve().parents[1] / "src" / "llmdv" / "suites"


def collect(marker: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--llmdv-profile",
            str(PROFILE),
            str(SUITES),
            "-q",
            "--collect-only",
            "-m",
            marker,
        ],
        capture_output=True,
        text=True,
    )


# ----------------------------------------------------------------- the marker set


def test_every_suite_holding_cases_becomes_a_marker():
    names = suite_markers()
    assert "discovery" in names and "unhappy" in names and "contract" in names


def test_a_package_with_no_cases_is_not_a_marker():
    """`suites/engine/` and friends hold helpers, not cases; they must not look selectable."""
    assert "engine" not in suite_markers()


def test_known_markers_covers_both_the_declared_and_the_derived():
    known = known_markers()
    assert {"disruptive", "surface", "case_id"} <= known, "the hand-written ones"
    assert {"discovery", "openai_chat"} <= known, "the suite ones"


def _fake_item(module_name: str):
    return SimpleNamespace(module=SimpleNamespace(__name__=module_name))


def test_the_suite_is_read_from_the_module_path():
    assert _suite_of(_fake_item("llmdv.suites.discovery.test_models")) == "discovery"


def test_a_module_outside_the_suites_package_has_no_suite():
    assert _suite_of(_fake_item("tests.test_something")) is None


def test_an_item_with_no_module_does_not_explode():
    assert _suite_of(SimpleNamespace()) is None


# ----------------------------------------------------------------- selection


@pytest.mark.parametrize("marker", ["discovery", "unhappy", "openai_chat"])
def test_a_suite_name_now_selects_that_suite(marker):
    out = collect(marker)
    assert "no tests collected" not in out.stdout, f"-m {marker} selected nothing"


def test_an_unknown_marker_is_a_usage_error_not_an_empty_run():
    """The failure mode this file exists for: a typo must not publish a smaller green run."""
    out = collect("discovry")
    assert out.returncode != 0
    assert "no such marker" in out.stdout + out.stderr


def test_the_error_lists_what_would_have_worked():
    out = collect("discovry")
    assert "discovery" in out.stdout + out.stderr, "name the near miss the operator meant"


def test_boolean_expressions_are_still_accepted():
    """`not disruptive` must not be read as a marker named `not`."""
    assert "no such marker" not in collect("not disruptive").stdout


def test_one_bad_name_in_a_compound_expression_is_still_caught():
    out = collect("contract or discovry")
    assert "no such marker" in out.stdout + out.stderr


def _item(*needs: str) -> SimpleNamespace:
    marker = SimpleNamespace(args=needs)
    return SimpleNamespace(iter_markers=lambda name: [marker] if name == "needs" else [])


def test_a_known_capability_name_is_accepted():
    _check_capability_names([_item("messages_checks:effort_effect", "tools", "count_tokens")])


def test_a_misspelt_capability_field_is_a_usage_error():
    """A typo here would otherwise skip every case using it, on every fleet, for good.

    `_has_capability` answers False for a name it cannot resolve, which is the same answer it
    gives for a fleet that has not declared the capability -- so the report reads as honest
    coverage. Same reasoning as the `-m` check above.
    """
    with pytest.raises(pytest.UsageError, match="messages_check:effort_effect"):
        _check_capability_names([_item("messages_check:effort_effect")])


def test_a_misspelt_capability_value_is_a_usage_error():
    with pytest.raises(pytest.UsageError, match="messages_checks:effort_efect"):
        _check_capability_names([_item("messages_checks:effort_efect")])


def test_an_unknown_bare_capability_is_a_usage_error():
    with pytest.raises(pytest.UsageError, match="count_token"):
        _check_capability_names([_item("count_token")])


def test_registering_a_second_client_keeps_the_first_one_s_exchanges():
    """A cross-layer case talks to two ingresses; the evidence must show both.

    `_ACTIVE` held one client and `register_client` replaced it, so the engine's exchange never
    reached exchanges.jsonl or the failure bundle -- for the one case whose purpose is comparing
    two layers.
    """
    node = SimpleNamespace(
        nodeid="tests::two_layers",
        get_closest_marker=lambda name: None,
        own_markers=[],
        iter_markers=lambda name=None: [],
        iter_markers_with_node=lambda name=None: [],
    )
    engine = SimpleNamespace(exchanges=["engine"], last=None)
    gateway = SimpleNamespace(exchanges=["gateway"], last=None)
    first = SimpleNamespace(id="vllm")
    second = SimpleNamespace(id="litellm")
    try:
        register_client(node, first, engine)
        register_client(node, second, gateway)
        state = _ACTIVE[node.nodeid]
        assert [c.exchanges[0] for c in state["clients"]] == ["engine", "gateway"]
        # The verdict still belongs to the ingress registered last.
        assert state["client"] is gateway
        assert state["ingress"] is second
    finally:
        _ACTIVE.pop(node.nodeid, None)


def test_registering_the_same_client_twice_does_not_duplicate_its_exchanges():
    node = SimpleNamespace(
        nodeid="tests::same_client",
        get_closest_marker=lambda name: None,
        own_markers=[],
        iter_markers=lambda name=None: [],
        iter_markers_with_node=lambda name=None: [],
    )
    http = SimpleNamespace(exchanges=["one"], last=None)
    try:
        register_client(node, SimpleNamespace(id="a"), http)
        register_client(node, SimpleNamespace(id="a"), http)
        assert len(_ACTIVE[node.nodeid]["clients"]) == 1
    finally:
        _ACTIVE.pop(node.nodeid, None)


def test_a_case_can_attach_a_file_to_its_own_evidence_bundle():
    try:
        register_artifact("tests::artifact", "session-trace.jsonl", "{}\n")
        assert _ACTIVE["tests::artifact"]["artifacts"] == {"session-trace.jsonl": "{}\n"}
    finally:
        _ACTIVE.pop("tests::artifact", None)
