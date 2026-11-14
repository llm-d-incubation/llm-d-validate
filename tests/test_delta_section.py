"""The delta view: cases whose answer changed between layers.

A 60-row matrix hides the finding. The rows that matter are the ones where one layer answered
differently from another, because that difference IS the layer's contribution.
"""

from __future__ import annotations

from llmdv.cli.verify_cmd import _delta_section, _per_layer_counts

ORDER = ["gateway", "router", "engine"]


def test_a_case_that_changes_between_layers_is_listed():
    grid = {"openai_chat.basic": {"engine": "pass", "router": "pass", "gateway": "fail"}}
    text = "\n".join(_delta_section(grid, ORDER))
    assert "openai_chat.basic" in text
    assert "gateway" in text


def test_a_case_with_the_same_result_everywhere_is_not_listed():
    grid = {"openai_chat.basic": {"engine": "pass", "router": "pass", "gateway": "pass"}}
    assert "openai_chat.basic" not in "\n".join(_delta_section(grid, ORDER))


def test_a_skip_is_not_reported_as_a_divergence():
    """`skip:undeclared` means the case did not run there, not that the layer differed.

    Without this, every surface a gateway does not expose reads as a regression at that layer.
    """
    grid = {"unhappy.bad_credentials": {"engine": "skip:undeclared", "gateway": "pass"}}
    assert "bad_credentials" not in "\n".join(_delta_section(grid, ORDER))


def test_two_failures_of_the_same_kind_are_not_a_divergence():
    grid = {"unhappy.malformed_json": {"engine": "fail", "router": "fail", "gateway": "fail"}}
    assert "malformed_json" not in "\n".join(_delta_section(grid, ORDER))


def test_no_divergence_says_so_rather_than_printing_an_empty_table():
    grid = {"openai_chat.basic": {"engine": "pass", "gateway": "pass"}}
    assert "No case changed result between layers" in "\n".join(_delta_section(grid, ORDER))


def test_a_single_layer_run_produces_no_section_at_all():
    """One column cannot diverge from itself; a section here would be noise."""
    grid = {"openai_chat.basic": {"engine": "pass"}}
    assert _delta_section(grid, ["engine"]) == []


def test_counts_are_reported_per_layer_in_chain_order():
    records = [
        {"ingress": "gateway", "outcome": "pass"},
        {"ingress": "gateway", "outcome": "fail"},
        {"ingress": "engine", "outcome": "pass"},
        {"ingress": "engine", "outcome": "skip", "skip_class": "undeclared"},
    ]
    text = "\n".join(_per_layer_counts(records, ["gateway", "engine"]))
    assert text.index("gateway") < text.index("engine")
    assert "1 passed" in text and "1 failed" in text


def test_a_skip_class_is_named_in_the_per_layer_counts():
    records = [
        {"ingress": "gateway", "outcome": "pass"},
        {"ingress": "engine", "outcome": "skip", "skip_class": "undeclared"},
    ]
    assert "undeclared" in "\n".join(_per_layer_counts(records, ["gateway", "engine"]))


def test_a_single_layer_run_produces_no_per_layer_section():
    """The run totals already say this; repeating them per layer is noise."""
    records = [{"ingress": "engine", "outcome": "pass"}]
    assert _per_layer_counts(records, ["engine"]) == []
