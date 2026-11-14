"""The closing banner's per-layer block, and the ordering it shares with summary.md.

The run totals answer "did this fleet hold". They cannot answer "which layer did not": a run
reporting 19 failures across a gateway and an engine looks identical whether the gateway broke
everything or the engine did. Until this block existed that answer lived only in summary.md, so
anyone watching a rollout in a terminal had to open a file to get it.
"""

import textwrap
from pathlib import Path

import pytest

from llmdv.cli import banner
from llmdv.cli.verify_cmd import layer_order
from llmdv.profile import loader


def _rec(ingress: str, outcome: str, skip_class: str | None = None) -> dict:
    record = {"case_id": "case", "ingress": ingress, "outcome": outcome}
    if skip_class:
        record["skip_class"] = skip_class
    return record


def _profile(tmp_path, kinds: list[tuple[str, str]]):
    """Built through the loader, so the test cannot drift from the real schema."""
    blocks = "\n".join(
        textwrap.dedent(f"""\
              - id: {ingress_id}
                kind: {kind}
                base_url: http://localhost:800{n}
                surfaces: [chat_completions]""")
        for n, (ingress_id, kind) in enumerate(kinds)
    )
    path = tmp_path / "p.yaml"
    path.write_text(
        f"schema: llmdv.profile/1\nname: p\nmodel: {{public_id: m}}\ningresses:\n{blocks}\n"
    )
    return loader.load(path)


def _banner(monkeypatch, records: list[dict], layers: list[str] | None) -> str:
    lines: list[str] = []
    monkeypatch.setattr(banner, "emit", lambda s="": lines.append(s))
    banner.finish(Path("/tmp/irrelevant"), records, [], 1.0, layers=layers)
    return "\n".join(lines)


# ----------------------------------------------------------------- layer_order


def test_the_order_is_outermost_first_whatever_the_records_say(tmp_path):
    """Records arrive in execution order; the report reads in request order."""
    records = [_rec("engine", "pass"), _rec("gateway", "pass")]
    profile = _profile(tmp_path, [("engine", "vllm"), ("gateway", "litellm")])
    assert layer_order(records, profile) == ["gateway", "engine"]


def test_an_undeclared_ingress_keeps_its_place_rather_than_vanishing(tmp_path):
    """It ran, so it has a column. Dropping it would hide a result that exists."""
    records = [_rec("gateway", "pass"), _rec("mystery", "fail")]
    profile = _profile(tmp_path, [("gateway", "litellm")])
    assert layer_order(records, profile) == ["gateway", "mystery"]


def test_without_a_profile_the_order_is_first_seen():
    assert layer_order([_rec("b", "pass"), _rec("a", "pass")], None) == ["b", "a"]


# ----------------------------------------------------------------- banner block


def test_two_layers_get_a_tally_each(monkeypatch):
    records = [_rec("gateway", "pass"), _rec("gateway", "fail"), _rec("epp", "pass")]
    text = _banner(monkeypatch, records, ["gateway", "epp"])
    assert "per layer" in text
    assert text.index("gateway") < text.index("epp")


def test_a_single_layer_run_gets_no_block(monkeypatch):
    """The totals already say it; a second identical tally invites a hunt for a difference."""
    assert "per layer" not in _banner(monkeypatch, [_rec("router", "pass")], ["router"])


def test_skip_classes_are_named_per_layer(monkeypatch):
    """`gateway skipped 28` and `28 excluded_by_policy` mean very different things."""
    records = [_rec("gateway", "pass"), _rec("epp", "skip", "excluded_by_policy")]
    assert "excluded_by_policy" in _banner(monkeypatch, records, ["gateway", "epp"])


def test_a_layer_that_reported_nothing_is_not_printed_as_zeros(monkeypatch):
    """A row of zeros reads as a layer that ran and found nothing. That is not what happened."""
    text = _banner(monkeypatch, [_rec("gateway", "pass")], ["gateway", "epp"])
    assert "epp" not in text[text.index("per layer") :]


@pytest.mark.parametrize("layers", [None, []])
def test_no_layer_information_means_no_block(monkeypatch, layers):
    assert "per layer" not in _banner(monkeypatch, [_rec("gateway", "pass")], layers)
