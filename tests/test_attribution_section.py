"""A run only prints the attribution ladder when it can support one.

The ladder is a comparison between columns, and it also claims those columns sit in a known
order. With one ingress there is nothing to compare; with several the profile never declared
there is no order to read the comparison against. In both cases printing the table anyway
invites a reader to blame a layer on the strength of a claim nobody made. The run is still a
good result -- it answers a narrower question -- so the summary says which question it answered,
and names the layers that reported, which every other section of the page does too.
"""

from __future__ import annotations

import json
import textwrap

from llmdv.cli.verify_cmd import _attribution_section, _read_records, _summarise
from llmdv.profile import loader

# The rows of the ladder that used to be hardcoded. Nothing may print them now: they name
# layers from one fleet, whatever the profile in front of llmdv actually declares. Kept as a
# literal so a reintroduction is caught by text, not by shape.
LADDER_ROWS = ("direct vLLM fails", "direct passes, router fails", "router passes, LiteLLM fails")


def write_results(tmp_path, ingresses):
    """A minimal results.jsonl: the summary's decision is about how many ingresses reported."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
        for name in ingresses:
            fh.write(
                json.dumps(
                    {
                        "case_id": "openai_chat.basic_non_streaming",
                        "outcome": "pass",
                        "ingress": name,
                        "profile": "fake",
                        "run_id": "r",
                    }
                )
                + "\n"
            )
    return run_dir


def test_one_ingress_omits_the_ladder_and_says_why():
    lines = "\n".join(_attribution_section(["litellm"]))

    assert "## attribution" in lines
    for row in LADDER_ROWS:
        assert row not in lines, f"the ladder leaked into a single-ingress section: {row!r}"
    assert "Not available in this run" in lines
    assert "litellm" in lines, "it should name the ingress that was exercised"


def test_one_ingress_does_not_claim_a_failure_isolates_a_layer():
    lines = "\n".join(_attribution_section(["litellm"]))

    assert "does not say which component" in lines
    # The evidence is still kept even though this run cannot act on it.
    assert "served_by" in lines


def test_two_ingresses_without_a_profile_get_no_invented_ladder():
    """No profile means no declared order, and a ladder is an ordering claim.

    This used to print a fixed `direct vLLM -> router -> LiteLLM` table whatever had run. The
    literal assertions below are what would fail if that table came back.
    """
    lines = "\n".join(_attribution_section(["vllm", "router"]))

    assert "Not available in this run" in lines
    assert "direct vLLM" not in lines
    assert "LiteLLM mapping" not in lines
    for row in LADDER_ROWS:
        assert row not in lines, f"an invented ladder row survived: {row!r}"
    assert "| pattern |" not in lines
    # What ran is named: the grid above labels these columns anyway.
    assert "`vllm`" in lines and "`router`" in lines
    # And not confused with the one-column run, which is a different, true statement.
    assert "One ingress" not in lines
    assert "served_by" in lines


def test_no_ingress_at_all_does_not_claim_one_was_exercised():
    """A run that recorded no ingress -- every case skipped before a client was built.

    The first version of this test only asserted the ladder was absent, which passed while the
    text read "One ingress was exercised (`the single declared ingress`)". Asserting what the
    prose must NOT say is what catches that.
    """
    lines = "\n".join(_attribution_section([]))

    assert "Not available in this run" in lines
    assert "No ingress reported a result" in lines
    assert "One ingress was exercised" not in lines
    assert "the single declared ingress" not in lines
    for row in LADDER_ROWS:
        assert row not in lines


def test_summary_md_reflects_the_single_ingress_case(tmp_path):
    run_dir = write_results(tmp_path, ["litellm"])
    _summarise(run_dir, _read_records(run_dir))
    text = (run_dir / "summary.md").read_text(encoding="utf-8")

    assert "## attribution" in text
    assert "Not available in this run" in text
    for row in LADDER_ROWS:
        assert row not in text


def test_summary_md_invents_no_ladder_for_two_ingresses_without_a_profile(tmp_path):
    """The same through `_summarise`, which is how the hardcoded table stayed reachable.

    `RunState.profile` is `Profile | None`, so this is the real path, not a unit-test-only one.
    """
    run_dir = write_results(tmp_path, ["vllm", "router"])
    _summarise(run_dir, _read_records(run_dir))
    text = (run_dir / "summary.md").read_text(encoding="utf-8")

    assert "direct vLLM" not in text
    assert "LiteLLM mapping" not in text
    for row in LADDER_ROWS:
        assert row not in text
    assert "One ingress" not in text, "a two-column summary must not claim one column"
    assert "`vllm`" in text and "`router`" in text


def chain_profile(tmp_path, kinds: list[tuple[str, str]], name: str = "chain"):
    """A profile with one ingress per (id, kind) pair, for ordering assertions."""
    blocks = "\n".join(
        textwrap.dedent(f"""\
              - id: {ingress_id}
                kind: {kind}
                base_url: http://localhost:800{n}
                surfaces: [chat_completions]""")
        for n, (ingress_id, kind) in enumerate(kinds)
    )
    path = tmp_path / f"{name}.yaml"
    path.write_text(
        f"schema: llmdv.profile/1\nname: {name}\nmodel: {{public_id: m}}\ningresses:\n{blocks}\n"
    )
    return loader.load(path)


def test_the_chain_orders_outermost_first_whatever_the_declaration_order(tmp_path):
    """Order comes from kind, so a profile listing the engine first still reports gateway first."""
    profile = chain_profile(
        tmp_path, [("engine", "vllm"), ("gateway", "litellm"), ("router", "llmd_router")]
    )
    assert profile.chain() == ["gateway", "router", "engine"]


def test_a_single_ingress_chain_is_just_that_ingress(tmp_path):
    profile = chain_profile(tmp_path, [("router", "llmd_router")], name="one")
    assert profile.chain() == ["router"]


def test_the_ladder_names_the_declared_layers(tmp_path):
    profile = chain_profile(
        tmp_path, [("engine", "vllm"), ("router", "llmd_router"), ("gateway", "litellm")]
    )
    lines = "\n".join(_attribution_section(["engine", "router", "gateway"], profile))
    assert "chain, outermost first: gateway -> router -> engine" in lines
    assert "engine fails" in lines
    assert "engine passes, router fails" in lines
    assert "router passes, gateway fails" in lines


def test_a_non_adjacent_pair_gets_one_row_and_no_invented_layer(tmp_path):
    """A gateway and an engine with no router between them is one comparison, not two."""
    profile = chain_profile(tmp_path, [("engine", "vllm"), ("gateway", "litellm")], name="two")
    lines = "\n".join(_attribution_section(["engine", "gateway"], profile))
    assert "router" not in lines.lower()
    assert "engine passes, gateway fails" in lines
    assert lines.count("passes,") == 1


def test_a_single_ingress_run_prints_no_ladder(tmp_path):
    """Already true, and must stay true now the rows are generated."""
    profile = chain_profile(tmp_path, [("router", "llmd_router")], name="solo")
    lines = "\n".join(_attribution_section(["router"], profile))
    assert "Not available in this run" in lines
    assert "passes," not in lines


def test_a_profile_mismatch_produces_no_ladder_and_invents_no_layer(tmp_path):
    """Summarising against a profile that does not declare the recorded ingresses must not crash.

    [n for n in profile.chain() if n in ingresses] comes out empty here -- the profile only
    declares "router", and neither recorded ingress is "router" -- and taking [-1] of that is an
    IndexError. It must also not fall through to a hardcoded ladder naming "router", a layer
    this run never touched and this profile does not relate to the two that reported.

    The ingresses that DID report are named. They produced the columns above; the grid, the
    per-layer tally and the delta table all label them already, and a section that refused to
    say which layers it was talking about would just be a worse version of those. What is
    withheld is the ordering claim, which is the part the profile never supplied.
    """
    profile = chain_profile(tmp_path, [("router", "llmd_router")], name="mismatch")
    lines = "\n".join(_attribution_section(["gateway", "engine"], profile))
    assert "Not available in this run" in lines
    assert "passes," not in lines
    assert "| pattern |" not in lines
    assert "router" not in lines.lower(), "a layer that neither ran nor relates to these two"
    assert "`gateway`" in lines
    assert "`engine`" in lines
    assert "One ingress" not in lines, "two ingresses reported; the grid above shows both"


def test_a_single_overlapping_layer_still_names_both_layers_that_ran(tmp_path):
    """The declared set can overlap the run by exactly one layer: still no order over the pair.

    One declared name is not a chain. The run reported `gateway` and `router`, so both are
    named -- the undeclared one is not a secret, it has a column -- and the section says the
    profile declares only one of them, which is why no ordering between them is offered.

    This is the case that used to answer differently from the zero-overlap test above, from the
    same function and the same question. They now agree.
    """
    profile = chain_profile(tmp_path, [("engine", "vllm"), ("gateway", "litellm")], name="partial")
    lines = "\n".join(_attribution_section(["gateway", "router"], profile))
    assert "Not available in this run" in lines
    assert "passes," not in lines
    assert "| pattern |" not in lines
    assert "`gateway`" in lines
    assert "`router`" in lines
    assert "declares only `gateway`" in lines
    assert "One ingress" not in lines


def test_the_ladder_is_the_only_section_that_withholds_a_name(tmp_path):
    """The restated rule, pinned: name what ran, never invent an order over it.

    Two undeclared ingresses and one undeclared ingress are the same question, and the one
    thing neither may produce is a pattern table.
    """
    none_declared = chain_profile(tmp_path, [("other", "vllm")], name="none")
    one_declared = chain_profile(
        tmp_path, [("engine", "vllm"), ("gateway", "litellm")], name="one-of"
    )
    for profile in (none_declared, one_declared):
        lines = "\n".join(_attribution_section(["gateway", "router"], profile))
        assert "`gateway`" in lines and "`router`" in lines
        assert "| pattern |" not in lines


def test_summary_md_orders_columns_by_the_chain_not_by_the_records(tmp_path):
    """The report's columns follow the declared chain, outermost first.

    The profile here declares the engine first and the gateway second, and the records arrive
    in that same innermost-first order, so anything that merely preserved either order would
    put `engine` on the left. Only the chain reorder in `_summarise` puts the gateway there --
    delete those three lines and this test fails.
    """
    profile = chain_profile(tmp_path, [("engine", "vllm"), ("gateway", "litellm")], name="order")
    run_dir = write_results(tmp_path, ["engine", "gateway"])
    _summarise(run_dir, _read_records(run_dir), profile=profile)
    text = (run_dir / "summary.md").read_text(encoding="utf-8")

    header = next(line for line in text.splitlines() if line.startswith("| case"))
    columns = [cell.strip() for cell in header.strip("|").split("|")]
    assert columns == ["case", "gateway", "engine"], header
