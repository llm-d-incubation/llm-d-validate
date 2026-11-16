"""`-m` must select what the operator meant, or say it could not.

Suite package names look like markers. Before this they were not, so `-m discovery` selected
nothing, pytest reported `72 deselected`, and the run completed and published as PASSED over a
smaller set than anybody asked for. A narrower green run is worse than an error, which is the
same reasoning that makes `banner.finish` refuse to print `PASSED | 0 cases`.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from llmdv.plugin import (
    _ACTIVE,
    PREFIX_EXCEPTIONS,
    _check_capability_names,
    _check_case_prefixes,
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


# ----------------------------------------------------------------- case_id prefixes

# Prints, for every collected case, its case_id and markers. Read from a child process so the
# markers are the ones the plugin applied during a real collection.
_DUMP_MARKERS = """
import json, sys, pytest

class Dump:
    def pytest_collection_finish(self, session):
        rows = []
        for item in session.items:
            mark = item.get_closest_marker("case_id")
            rows.append({
                "nodeid": item.nodeid,
                "case_id": mark.args[0] if mark and mark.args else None,
                "markers": sorted({m.name for m in item.iter_markers()}),
            })
        sys.stdout.write("ROWS " + json.dumps(rows) + "\\n")

pytest.main(sys.argv[1:], plugins=[Dump()])
"""


def collected(*args: str) -> list[dict]:
    out = subprocess.run(
        [sys.executable, "-c", _DUMP_MARKERS, "--llmdv-profile", str(PROFILE), str(SUITES)]
        + ["-q", "--collect-only", "-p", "no:cacheprovider", *args],
        capture_output=True,
        text=True,
    )
    line = next((x for x in out.stdout.splitlines() if x.startswith("ROWS ")), None)
    assert line, out.stdout + out.stderr
    return json.loads(line[len("ROWS ") :])


def test_a_prefix_naming_a_suite_selects_the_case_wherever_it_lives():
    """The output shows `openai_chat.disconnect_recovery` under openai_chat; `-m` must agree."""
    ids = {row["case_id"] for row in collected("-m", "openai_chat")}
    assert "openai_chat.disconnect_recovery" in ids
    assert not [i for i in ids if i.startswith("streaming.")], "the rest of streaming stays out"


def test_the_package_still_selects_the_cases_it_selected_before():
    """The prefix marker only adds: `-m streaming` keeps the disconnect cases."""
    ids = {row["case_id"] for row in collected("-m", "streaming")}
    assert "openai_chat.disconnect_recovery" in ids
    assert "transport.stream_encoding" in ids


def test_every_case_keeps_its_package_marker_and_gains_only_its_prefix_suite():
    # `contract` is also a hand-written marker that cases outside contract/ carry on purpose.
    suites = set(suite_markers()) - {"contract"}
    rows = collected()
    assert rows
    for row in rows:
        package = row["nodeid"].split("/suites/", 1)[1].split("/", 1)[0]
        prefix = (row["case_id"] or "").split(".", 1)[0] if "." in (row["case_id"] or "") else None
        assert package in row["markers"], f"{row['nodeid']} lost its package marker"
        expected = ({package} | {prefix}) & suites
        assert set(row["markers"]) & suites == expected, row


def _case(module: str, case_id: str | None) -> SimpleNamespace:
    marker = SimpleNamespace(args=(case_id,)) if case_id else None
    return SimpleNamespace(
        module=SimpleNamespace(__name__=module),
        nodeid=f"{module.replace('.', '/')}.py::test_case",
        get_closest_marker=lambda name: marker if name == "case_id" else None,
    )


def test_a_prefix_matching_its_package_passes():
    _check_case_prefixes([_case("llmdv.suites.discovery.test_models", "discovery.lists_model")])


def test_a_reviewed_mismatch_passes():
    _check_case_prefixes(
        [_case("llmdv.suites.streaming.test_disconnect", "openai_chat.disconnect_recovery")]
    )


def test_a_case_outside_the_suites_or_without_a_prefix_is_not_checked():
    _check_case_prefixes([_case("tests.test_something", "cache.anything")])
    _check_case_prefixes([_case("llmdv.suites.streaming.test_x", "no_dot")])
    _check_case_prefixes([_case("llmdv.suites.streaming.test_x", None)])


def test_a_new_mismatch_names_the_case_its_prefix_and_its_package():
    """A reviewed pair does not license the same prefix in another package."""
    assert "openai_chat" not in PREFIX_EXCEPTIONS.get("tools", {})
    with pytest.raises(pytest.UsageError) as raised:
        _check_case_prefixes([_case("llmdv.suites.tools.test_new", "openai_chat.drifted")])
    message = str(raised.value)
    assert "llmdv/suites/tools/test_new.py::test_case" in message
    assert "'openai_chat'" in message and "'tools'" in message
    assert "PREFIX_EXCEPTIONS" in message


def test_every_reviewed_mismatch_has_a_reason():
    for package, prefixes in PREFIX_EXCEPTIONS.items():
        assert prefixes, package
        for prefix, reason in prefixes.items():
            assert prefix != package and reason.strip(), (package, prefix)


def test_a_new_mismatch_fails_a_real_collection_even_when_m_would_deselect_it(tmp_path):
    """Checked before `-m` filters, so narrowing a run cannot hide the drift."""
    package = tmp_path / "fakepkg" / "suites" / "streaming"
    package.mkdir(parents=True)
    for directory in (tmp_path / "fakepkg", package.parent, package):
        (directory / "__init__.py").write_text("")
    (package / "test_new.py").write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.mark.case_id("cache.drifted")
            def test_drifted():
                pass

            @pytest.mark.case_id("streaming.fine")
            def test_fine():
                pass
            """
        )
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "fakepkg",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ]
        + ["-m", "streaming"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0
    text = out.stdout + out.stderr
    assert "test_new.py::test_drifted" in text
    assert "'cache'" in text and "'streaming'" in text


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
