"""The cross-ingress case, proven to fail where two ingresses do not share one prefix."""

import json
from copy import deepcopy

import pytest
import yaml

from tests.fakes import FakeFleet
from tests.test_cache_reuse import BLOCKS, RATIO
from tests.test_suites_against_fake import run_suites, write_profile

PAIR = ["fake", "second"]


def run_pair(
    tmp_path,
    declaration,
    *,
    defect=None,
    shared=True,
    shared_with=PAIR,
    peer_cache_surface="chat_completions",
    peer_options=None,
):
    """The case on Chat through two ingresses, the second a layer over the first's engines."""
    with FakeFleet(cache_probe=True, cache_block=16) as first:
        store = first.cache_store if shared else None
        with FakeFleet(
            cache_probe=True,
            cache_block=16,
            cache_defect=defect,
            shared_cache=store,
            **(peer_options or {}),
        ) as second:
            path = write_profile(tmp_path, first, responses=False)
            raw = yaml.safe_load(path.read_text())
            raw["ingresses"][0]["surfaces"] = ["chat_completions"]
            other = deepcopy(raw["ingresses"][0])
            other.update(id="second", base_url=second.base_url)
            if peer_cache_surface is None:
                other["capabilities"] = {"cache_reuse": None}
            elif peer_cache_surface != "chat_completions":
                other["capabilities"] = {
                    "cache_reuse": {
                        "surfaces": [peer_cache_surface],
                        "prefix_words": 128,
                        **declaration,
                    }
                }
            raw["ingresses"].append(other)
            cache = {"surfaces": ["chat_completions"], "prefix_words": 128, **declaration}
            raw["capabilities"] = {"cache_reuse": {**cache, "shared_with": shared_with}}
            path.write_text(yaml.safe_dump(raw))
            code, records = run_suites(path, tmp_path / "run", "-k", "test_cross_ingress_reuse")
    return code, {
        r["ingress"]: r
        for r in records
        if r["case_id"] == "cache.cross_ingress" and r["surface"] == "chat_completions"
    }


@pytest.mark.parametrize(
    "declaration,defect,shared,outcome",
    [
        (BLOCKS, None, True, "pass"),
        (RATIO, None, True, "pass"),
        # The second layer adds its own text ahead of the prompt it forwards.
        (BLOCKS, "layer_rewrites", True, "fail"),
        (RATIO, "layer_rewrites", True, "fail"),
        # Declared shared, but the second ingress fronts other engines.
        (BLOCKS, None, False, "fail"),
    ],
)
def test_cross_ingress_reads_the_other_ingress(tmp_path, declaration, defect, shared, outcome):
    code, records = run_pair(tmp_path, declaration, defect=defect, shared=shared)
    assert code == (1 if outcome == "fail" else 0), records
    assert {k: r["outcome"] for k, r in records.items()} == {"fake": outcome, "second": outcome}


def test_cross_ingress_exchanges_keep_their_peer_attribution(tmp_path):
    run_pair(tmp_path, BLOCKS)
    profile = yaml.safe_load((tmp_path / "fake.yaml").read_text())
    bases = {entry["id"]: entry["base_url"] for entry in profile["ingresses"]}
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    exchanges = [json.loads(line) for line in lines]
    exchanges = [
        exchange for exchange in exchanges if exchange.get("case_id") == "cache.cross_ingress"
    ]
    assert {exchange["ingress"] for exchange in exchanges} == {"fake", "second"}
    for exchange in exchanges:
        assert exchange["url"].startswith(bases[exchange["ingress"]])
        assert exchange["ingress_kind"] == "vllm"


def test_cross_ingress_skips_an_ingress_outside_the_group(tmp_path):
    code, records = run_pair(tmp_path, BLOCKS, shared_with=["second"])
    assert code == 0, records
    assert {r["outcome"] for r in records.values()} == {"skip"}, records


@pytest.mark.parametrize("peer_cache_surface", [None, "responses"])
def test_cross_ingress_skips_a_peer_without_compatible_cache_reuse(tmp_path, peer_cache_surface):
    code, records = run_pair(tmp_path, BLOCKS, peer_cache_surface=peer_cache_surface)
    assert code == 0, records
    assert set(records) == {"fake", "second"}, records
    assert {r["outcome"] for r in records.values()} == {"skip"}, records
    assert all("[undeclared]" in r["reason"] for r in records.values()), records


def test_peer_failure_is_attributed_to_the_ingress_under_test(tmp_path):
    code, records = run_pair(
        tmp_path,
        BLOCKS,
        peer_options={"model_errors": {"fake/Model-1B": 500}},
    )
    assert code == 1, records
    assert set(records) == {"fake", "second"}, records
    assert records["fake"]["outcome"] == records["second"]["outcome"] == "fail"


def test_native_repeat_misses_a_rewriting_layer(tmp_path):
    """Each ingress repeats its own prompt, so a layer that rewrites it alike stays warm."""
    with FakeFleet(cache_probe=True, cache_block=16, cache_defect="layer_rewrites") as fleet:
        path = write_profile(tmp_path, fleet, responses=False)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["chat_completions"]
        cache = {"surfaces": ["chat_completions"], "prefix_words": 128, **BLOCKS}
        raw["capabilities"] = {"cache_reuse": cache}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_native_cache_reuse")
    assert code == 0, records
    assert [r["outcome"] for r in records if r["surface"] == "chat_completions"] == ["pass"]


@pytest.mark.parametrize("shared_with", [["fake", "fake"], [""], "fake", [3]])
def test_shared_with_is_strict(tmp_path, shared_with):
    from llmdv.profile.loader import ProfileError, load
    from tests.test_cache_reuse import cache_profile

    path = cache_profile(
        tmp_path, {"min_ratio": 0.8, "single_replica": True, "shared_with": shared_with}
    )
    with pytest.raises(ProfileError, match="shared_with"):
        load(path)


def test_shared_with_an_unknown_ingress_is_a_profile_error(tmp_path):
    from llmdv.profile.loader import load, validate
    from tests.test_cache_reuse import cache_profile

    path = cache_profile(
        tmp_path, {"min_ratio": 0.8, "single_replica": True, "shared_with": ["missing"]}
    )
    errors = [d for d in validate(load(path)) if d.level == "error"]
    assert any("shared_with" in d.path and "missing" in d.message for d in errors), errors
