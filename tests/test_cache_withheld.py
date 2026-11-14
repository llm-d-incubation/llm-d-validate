"""`cache_reuse.withheld_blocks`: a deployment that withholds the trailing blocks of every hit.

An engine with a speculative-decoding draft recomputes the draft's KV for the last chunk of a
hit, so a warm repeat reads that many blocks fewer than its whole-block floor. The declaration
lowers the floor by exactly that much: one block more missing, or nothing read, still fails.
"""

from __future__ import annotations

import pytest

from llmdv.assertions.cache import assert_block_floor, block_floor
from llmdv.assertions.turns import assert_turn_floor, turn_floor
from llmdv.evidence import journal
from llmdv.profile.loader import ProfileError, load
from tests.test_cache_repeats import SURFACES, TOOLS
from tests.test_cache_reuse import BLOCKS, cache_profile, run_cache_case

WITHHELD = {**BLOCKS, "withheld_blocks": 1}


# The floor math, in the issue's own numbers: 512-token blocks, a 2482-token prompt.


@pytest.mark.parametrize(
    "prompt,withheld,floor",
    [
        (2482, 0, 2048),
        (2482, 1, 1536),
        (2482, 2, 1024),
        # The last token is always recomputed, so a whole-block prompt keeps one block back.
        (2048, 0, 1536),
        (2048, 1, 1024),
        # Never below zero.
        (600, 1, 0),
        (600, 3, 0),
    ],
)
def test_block_floor_drops_the_withheld_blocks(prompt, withheld, floor):
    assert block_floor(prompt, 512, withheld) == floor


@pytest.mark.parametrize(
    "slack,withheld,floor",
    [
        (1, 0, 1536),
        (2, 0, 1024),
        # Withholding comes off on top of the slack: the slack bounds where the hit ends, and
        # the deployment then reads a block fewer than that hit.
        (1, 1, 1024),
        (2, 1, 512),
        (3, 1, 0),
    ],
)
def test_turn_floor_adds_the_withheld_blocks_to_the_slack(slack, withheld, floor):
    assert turn_floor(2482, 512, slack, withheld) == floor


@pytest.mark.parametrize(
    "read,ok",
    [
        # Production until it moves to the new image, and any stack without the draft tagging.
        (2048, True),
        # The canary: exactly one block fewer.
        (1536, True),
        (1535, False),
        (1024, False),
        # A warm repeat that reads nothing is a miss, whatever is withheld.
        (0, False),
    ],
)
def test_the_block_floor_accepts_the_declared_allowance_and_no_more(read, ok):
    active = journal.activate("withheld")
    try:
        if ok:
            assert_block_floor("repeat", read, 2482, 512, 1)
        else:
            with pytest.raises(AssertionError, match="less 1 trailing block"):
                assert_block_floor("repeat", read, 2482, 512, 1)
    finally:
        journal.deactivate()
    (check,) = active.checks
    assert check.ok is ok
    # The verdict carries the declared allowance, not only the lowered number.
    assert check.expected == (
        "read >= 1536, less 1 trailing block the deployment declares withheld"
    )


def test_an_undeclared_allowance_keeps_the_full_floor_and_its_text():
    active = journal.activate("full")
    try:
        with pytest.raises(AssertionError, match="below the 2048-token floor"):
            assert_block_floor("repeat", 1536, 2482, 512)
    finally:
        journal.deactivate()
    assert active.checks[0].expected == "read >= 2048"


def test_a_prompt_no_longer_than_the_withheld_blocks_cannot_pass():
    """A lowered floor of zero would accept a read of nothing; it fails as too short to judge."""
    with pytest.raises(AssertionError, match="fills no 512-token block, less 1"):
        assert_block_floor("repeat", 512, 600, 512, 1)
    with pytest.raises(AssertionError, match="too few 512-token blocks to judge, less 1"):
        assert_turn_floor("turn", 512, 1100, 512, 1, 1)


def test_the_turn_floor_records_the_allowance():
    active = journal.activate("turn")
    try:
        assert_turn_floor("turn", 1024, 2482, 512, 1, 1)
        with pytest.raises(AssertionError):
            assert_turn_floor("turn", 1023, 2482, 512, 1, 1)
    finally:
        journal.deactivate()
    assert [c.ok for c in active.checks] == [True, False]
    assert "less 1 trailing block" in active.checks[0].expected


# The declaration.


def test_withheld_blocks_defaults_to_none_withheld(tmp_path):
    config = load(cache_profile(tmp_path, {**BLOCKS, "prefix_words": 128}))
    assert config.ingresses[0].capabilities.cache_reuse.withheld_blocks == 0


@pytest.mark.parametrize(
    "value,message",
    [
        (-1, "integer of zero or more"),
        (True, "integer of zero or more"),
        ("1", "integer of zero or more"),
        (1.0, "integer of zero or more"),
        (None, "withheld_blocks: cannot be null"),
        # 16-token blocks and a 128-word prefix: withholding all eight leaves nothing to read.
        (8, "more blocks than are withheld"),
    ],
)
def test_withheld_blocks_is_strict(tmp_path, value, message):
    path = cache_profile(tmp_path, {**BLOCKS, "prefix_words": 128, "withheld_blocks": value})
    with pytest.raises(ProfileError, match=message):
        load(path)


def test_withheld_blocks_needs_a_block_size(tmp_path):
    path = cache_profile(tmp_path, {"min_ratio": 0.8, "single_replica": True, "withheld_blocks": 1})
    with pytest.raises(ProfileError, match="withheld_blocks: lowers block floors only"):
        load(path)


def test_withheld_blocks_is_overridable_per_ingress(tmp_path):
    declared = {**BLOCKS, "prefix_words": 128, "withheld_blocks": 1}
    config = load(cache_profile(tmp_path, declared, engine_override={"withheld_blocks": 0}))
    assert config.capabilities.cache_reuse.withheld_blocks == 1
    assert config.ingresses[0].capabilities.cache_reuse.withheld_blocks == 0
    config = load(cache_profile(tmp_path, {**BLOCKS, "prefix_words": 128}, {"withheld_blocks": 2}))
    assert config.capabilities.cache_reuse.withheld_blocks == 0
    assert config.ingresses[0].capabilities.cache_reuse.withheld_blocks == 2


# The cases, against a fake engine that withholds trailing blocks of every hit.


def outcomes(records):
    ran = [r for r in records if r["outcome"] != "skip"]
    return {r["outcome"] for r in ran}, ran


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "declaration,withheld,outcome",
    [
        # The canary before this declaration: every warm repeat one block short.
        (BLOCKS, 1, "fail"),
        (WITHHELD, 1, "pass"),
        # Production reads the full floor until it moves to the new image.
        (WITHHELD, 0, "pass"),
        # One block withheld is declared; a second missing block is a defect.
        (WITHHELD, 2, "fail"),
    ],
)
def test_the_cache_cases_follow_the_declared_allowance(
    tmp_path, surface, declaration, withheld, outcome
):
    code, records = run_cache_case(
        tmp_path, surface, declaration, "cache", model=TOOLS, fake={"cache_withheld": withheld}
    )
    seen, ran = outcomes(records)
    assert code == (1 if outcome == "fail" else 0), records
    if outcome == "pass":
        assert seen == {"pass"}, records
        return
    floors = [
        c
        for r in ran
        if r["outcome"] == "fail"
        for c in r["checks"]
        if c["name"] == "cache_block_floor" and c.get("ok") is False
    ]
    # The repeat cases fail on the floor check itself, not on something else.
    assert floors, records
    assert "cache.native_reuse" in {r.get("case_id") for r in ran if r["outcome"] == "fail"}
    if declaration is WITHHELD:
        assert all("less 1 trailing block" in c["expected"] for c in floors), floors


@pytest.mark.parametrize("surface", SURFACES)
def test_a_warm_repeat_that_reads_nothing_still_fails(tmp_path, surface):
    code, records = run_cache_case(
        tmp_path,
        surface,
        WITHHELD,
        "test_native_cache_reuse",
        "cold",
        fake={"cache_withheld": 1},
    )
    assert code == 1, records
    assert records[0]["outcome"] == "fail", records
    assert "read 0 cached tokens" in records[0]["reason"], records


@pytest.mark.parametrize("declaration,outcome", [(BLOCKS, "fail"), (WITHHELD, "pass")])
def test_the_turn_cases_take_the_allowance_on_top_of_their_slack(tmp_path, declaration, outcome):
    """Two blocks withheld exhaust one block of slack; a declared one restores it.

    The fake renders each earlier turn's tail unchanged, so a turn hits every whole block of the
    earlier prompt and one withheld block fits inside the slack either way.
    """
    code, records = run_cache_case(
        tmp_path,
        "chat_completions",
        declaration,
        "test_turn_reuse",
        fake={"cache_withheld": 2},
    )
    seen, _ = outcomes(records)
    assert code == (1 if outcome == "fail" else 0), records
    assert seen == {outcome}, records
