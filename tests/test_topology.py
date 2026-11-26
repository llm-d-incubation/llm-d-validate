"""The layer graph, and the one pairing rule the summary ladder and the alert feed share.

Two things are pinned. First, compatibility: for every stack without two ingresses of one kind
-- every committed profile, and every ordering of the three kinds -- the chain, the ladder, the
attribution and the innermost ingress are exactly what the depth order gave before the graph
existed. The old readings are kept below as literal oracles, so a drift is caught by value.
Second, the fix: two ingresses of one kind are peers, and nothing compares one with the other.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from llmdv.cli.verify_cmd import _attribution_section
from llmdv.evidence import alerts
from llmdv.profile import compose, loader
from llmdv.profile.schema import IngressKind, Profile, depth
from llmdv.profile.topology import Topology
from llmdv.suites.fleet.conftest import innermost


def _depth_of(record: dict) -> int | None:
    try:
        return depth(record.get("ingress_kind") or "")
    except (KeyError, ValueError):
        return None


def _depth_reading(record: dict, column: dict[str, dict]) -> tuple[dict, str]:
    """The attribution the alert feed gave before the graph existed, kept here as a literal
    oracle: only layers strictly inside this one count, nearest first. Without peers the graph
    walk must agree with it exactly."""
    here = record.get("ingress") or "-"
    mine = _depth_of(record)

    def deeper(other: dict) -> bool:
        theirs = _depth_of(other)
        return mine is not None and theirs is not None and theirs > mine

    culprit = record
    for other in sorted(filter(deeper, column.values()), key=_depth_of):
        if other["outcome"] in ("fail", "error"):
            culprit = other
            continue
        return culprit, f"fails at {culprit.get('ingress')}, passes at {other.get('ingress')}"
    return alerts._innermost(culprit, here)


ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = sorted(
    [
        p
        for p in (ROOT / "profiles").rglob("*.yaml")
        if not p.name.endswith(".local.yaml") and not p.name.startswith(".")
    ]
    + [ROOT / "deploy" / "overlays" / "example" / "profile.yaml"]
)
OUTCOMES = ("pass", "fail", "error")
KINDS = (IngressKind.GATEWAY, IngressKind.ROUTER, IngressKind.ENGINE)


def standalone(path: Path) -> Profile | None:
    try:
        return loader.load(path)
    except loader.ProfileError:
        return None


COMMITTED = [(path, profile) for path in CANDIDATES if (profile := standalone(path))]


def stack(*layers: tuple[str, IngressKind]) -> Profile:
    """A committed profile with its ingresses replaced by `(id, kind)`, in declaration order."""
    base = COMMITTED[0][1]
    template = base.ingresses[0]
    ingresses = tuple(dataclasses.replace(template, id=i, kind=k) for i, k in layers)
    return dataclasses.replace(base, ingresses=ingresses)


# Every ordering of every subset of the three kinds, ids named after their declaration slot.
GENERATED = [
    stack(*((f"l{n}", kind) for n, kind in enumerate(kinds)))
    for size in (1, 2, 3)
    for kinds in itertools.permutations(KINDS, size)
]
NO_PEERS = [profile for _, profile in COMMITTED] + GENERATED


# ------------------------------------------------------------------ the readings before the graph


def old_chain(profile: Profile) -> list[str]:
    return [item.id for item in sorted(profile.ingresses, key=lambda i: depth(i.kind))]


def old_ladder(ingresses: list[str], profile: Profile) -> list[str]:
    declared = [name for name in old_chain(profile) if name in ingresses]
    rows = [f"| {declared[-1]} fails | backend, model template, or fixture |"]
    for outer, inner in zip(declared, declared[1:], strict=False):
        rows.append(
            f"| {inner} passes, {outer} fails | "
            f"{outer} parsing, mutation, routing, auth, or timeout |"
        )
    return [
        "## attribution (a hypothesis, not a proof)",
        "",
        "chain, outermost first: " + " -> ".join(declared),
        "",
        "| pattern | suggests |",
        "| --- | --- |",
        *reversed(rows),
        "",
    ]


def old_innermost(selected: set[str], profile: Profile) -> str:
    return [item for item in old_chain(profile) if item in selected][-1]


# ------------------------------------------------------------------------------------ helpers


def subsets(names, smallest=1):
    for size in range(smallest, len(names) + 1):
        yield from itertools.combinations(names, size)


def column(profile: Profile, outcomes: dict[str, str]) -> dict[str, dict]:
    kinds = {item.id: item.kind.value for item in profile.ingresses}
    return {
        name: {"ingress": name, "ingress_kind": kinds[name], "outcome": outcome}
        for name, outcome in outcomes.items()
    }


def config(selected) -> SimpleNamespace:
    return SimpleNamespace(getoption=lambda _: list(selected))


# ------------------------------------------------------------------------------ compatibility


def test_there_are_committed_profiles_to_check():
    """Guards the globs: a rename that emptied them would make the property below vacuous."""
    assert len(COMMITTED) >= 4, [str(p) for p, _ in COMMITTED]
    assert any(len(p.ingresses) == 3 for _, p in COMMITTED)


@pytest.mark.parametrize("profile", NO_PEERS, ids=lambda p: ",".join(p.chain()))
def test_without_peers_the_chain_ladder_and_innermost_are_unchanged(profile):
    assert profile.chain() == old_chain(profile)
    for reported in subsets(profile.ingress_ids):
        assert innermost(config(reported), profile) == old_innermost(set(reported), profile)
        if len(reported) < 2:
            continue
        want = old_ladder(list(reported), profile)
        assert _attribution_section(list(reported), profile)[: len(want)] == want


@pytest.mark.parametrize("profile", NO_PEERS, ids=lambda p: ",".join(p.chain()))
def test_without_peers_attribution_is_unchanged(profile):
    topology = profile.topology()
    for reported in subsets(profile.ingress_ids):
        for outcomes in itertools.product(OUTCOMES, repeat=len(reported)):
            # Declaration order and reversed: the old reading sorted by depth, so neither matters.
            for names in (reported, reported[::-1]):
                col = column(profile, dict(zip(names, outcomes, strict=True)))
                for record in col.values():
                    if record["outcome"] == "pass":
                        continue
                    want = _depth_reading(record, col)
                    assert alerts.attribute(record, col, topology) == want


def test_a_digest_is_not_moved_by_the_graph(tmp_path):
    """The graph is derived, never written into the profile, so the digest reads the same file."""
    for index, (path, profile) in enumerate(COMMITTED):
        # Copy the whole extends chain beside it, so a profile written as changes still loads.
        home = tmp_path / str(index)
        for layer in compose.layers(path):
            target = home / layer.path.resolve().relative_to(path.parent.resolve().parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(layer.path.read_text(encoding="utf-8"), encoding="utf-8")
        copy = home / path.resolve().relative_to(path.parent.resolve().parent)
        profile.topology()
        assert loader.contract_digest(copy) == loader.contract_digest(path)


def run_dir(tmp_path: Path, records: list[dict], topology: dict | None) -> Path:
    directory = tmp_path / ("with" if topology else "without")
    directory.mkdir()
    (directory / "results.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    body = {"run_id": "r", "verification": {"exit_code": 1, "class": "fleet", "counts": {}}}
    if topology is not None:
        body["topology"] = topology
    (directory / "outcome.json").write_text(json.dumps(body))
    return directory


def test_a_recorded_graph_gives_the_same_feed_as_depth_for_a_stack_without_peers(tmp_path):
    profile = stack(("engine", KINDS[2]), ("gateway", KINDS[0]), ("router", KINDS[1]))
    records = []
    for case, outcomes in enumerate(itertools.product(OUTCOMES, repeat=3)):
        col = column(profile, dict(zip(("gateway", "router", "engine"), outcomes, strict=True)))
        records += [{**r, "case_id": f"c{case}", "variant": "chat"} for r in col.values()]
    recorded = profile.topology().as_record()
    with_graph = alerts.build(run_dir(tmp_path, records, recorded))
    without = alerts.build(run_dir(tmp_path, records, None))
    assert json.dumps(with_graph) == json.dumps(without)


# ------------------------------------------------------------------------------ peers, the fix

PD = stack(("decode", KINDS[2]), ("epp", KINDS[1]), ("prefill", KINDS[2]))


def test_two_engines_are_peers_behind_the_router():
    topology = PD.topology()
    assert topology.order == ("epp", "decode", "prefill")
    assert dict(topology.fronts) == {"epp": ("decode", "prefill"), "decode": (), "prefill": ()}
    assert topology.pairs(["decode", "prefill"]) == []
    assert topology.pairs(["epp", "decode", "prefill"]) == [("epp", "decode"), ("epp", "prefill")]


def test_the_ladder_never_compares_two_engines():
    text = "\n".join(_attribution_section(["epp", "decode", "prefill"], PD))
    assert "decode passes, prefill fails" not in text
    assert "prefill passes, decode fails" not in text
    assert "| decode passes, epp fails |" in text
    assert "| prefill passes, epp fails |" in text
    assert "| decode fails |" in text and "| prefill fails |" in text
    assert "layers, outermost first: epp, decode, prefill" in text
    assert "compared along: epp -> decode, epp -> prefill" in text
    assert "chain, outermost first" not in text


def test_two_engines_alone_are_never_compared():
    text = "\n".join(_attribution_section(["decode", "prefill"], PD))
    assert "passes" not in text.split("| --- | --- |")[1].split("\n\n")[0]
    assert "compared along: none" in text


def test_the_feed_never_points_from_one_engine_to_its_peer(tmp_path):
    outcomes = {"epp": "fail", "decode": "pass", "prefill": "fail"}
    records = [{**r, "case_id": "c", "variant": "chat"} for r in column(PD, outcomes).values()]
    feed = alerts.build(run_dir(tmp_path, records, PD.topology().as_record()))
    got = {r["ingress"]: (r["points_at"], r["basis"]) for r in feed["results"] if r.get("basis")}
    assert got == {
        "epp": ("prefill", "fails at prefill, the innermost layer that ran this case, and at epp"),
        "prefill": ("prefill", "fails at prefill, the innermost layer that ran this case"),
    }


def test_a_layer_whose_peers_behind_it_all_pass_names_them_all():
    col = column(PD, {"epp": "fail", "decode": "pass", "prefill": "pass"})
    culprit, basis = alerts.attribute(col["epp"], col, PD.topology())
    assert culprit["ingress"] == "epp"
    assert basis == "fails at epp, passes at decode, prefill"


def test_a_run_without_a_graph_derives_one_and_never_compares_peers(tmp_path):
    """With no graph recorded, the feed derives one from the positions the results carry, by
    the rule a profile uses, so the failing prefill is never read against its passing peer."""
    outcomes = {"epp": "fail", "prefill": "fail", "decode": "pass"}
    records = [{**r, "case_id": "c", "variant": "chat"} for r in column(PD, outcomes).values()]
    feed = alerts.build(run_dir(tmp_path, records, None))
    (epp,) = [r for r in feed["results"] if r["ingress"] == "epp"]
    assert epp["basis"] == "fails at prefill, the innermost layer that ran this case, and at epp"


# ------------------------------------------------------------- a peer that did not report

# A gateway, two routers and an engine. `rb` is the router that reports nothing below.
WIDE = stack(("lite", KINDS[0]), ("ra", KINDS[1]), ("rb", KINDS[1]), ("e1", KINDS[2]))


def test_a_layer_is_not_compared_past_a_reported_layer_through_its_silent_peer():
    topology = WIDE.topology()
    assert topology.nearest("lite", ["lite", "ra", "e1"]) == ["ra"]
    assert topology.pairs(["lite", "ra", "e1"]) == [("lite", "ra"), ("ra", "e1")]
    # With both routers reporting, each is compared with the gateway and with the engine.
    assert topology.pairs(["lite", "ra", "rb", "e1"]) == [
        ("lite", "ra"),
        ("lite", "rb"),
        ("ra", "e1"),
        ("rb", "e1"),
    ]
    # With neither reporting, the gateway is compared with the engine behind them.
    assert topology.pairs(["lite", "e1"]) == [("lite", "e1")]


def test_the_ladder_with_a_silent_peer_reads_as_the_chain_that_reported():
    text = "\n".join(_attribution_section(["lite", "ra", "e1"], WIDE))
    assert "chain, outermost first: lite -> ra -> e1" in text
    assert "| e1 passes, lite fails |" not in text
    assert "| ra passes, lite fails |" in text
    assert "| e1 passes, ra fails |" in text


@pytest.mark.parametrize("outcomes", list(itertools.product(OUTCOMES, repeat=3)))
def test_the_feed_with_a_silent_peer_reads_as_the_chain_that_reported(outcomes):
    col = column(WIDE, dict(zip(("lite", "ra", "e1"), outcomes, strict=True)))
    for record in col.values():
        if record["outcome"] != "pass":
            want = _depth_reading(record, col)
            assert alerts.attribute(record, col, WIDE.topology()) == want


# Two of every kind: every reported subset of it has peers, silent peers, or skipped layers.
TWO_EACH = stack(*((f"{kind.value}{n}", kind) for kind in KINDS for n in (1, 2)))


@pytest.mark.parametrize("reported", list(subsets(TWO_EACH.chain())), ids=",".join)
def test_the_feed_and_the_ladder_agree_for_every_reported_subset(reported):
    """The feed's walk only ever takes a step the ladder prints as a row.

    And where no two reported layers share a kind, both read as the depth order did.
    """
    topology = TWO_EACH.topology()
    kinds = {item.id: item.kind for item in TWO_EACH.ingresses}
    pairs = topology.pairs(reported)
    compared: dict[str, list[str]] = {name: [] for name in reported}
    for outer, inner in pairs:
        compared[outer].append(inner)
    # The rule, stated by depth: the reported layers at the nearest deeper depth that has any.
    for name in reported:
        inside = [other for other in reported if depth(kinds[other]) > depth(kinds[name])]
        nearest = min((depth(kinds[other]) for other in inside), default=None)
        assert compared[name] == [o for o in inside if depth(kinds[o]) == nearest]
    if len(reported) > 1:
        text = "\n".join(_attribution_section(list(reported), TWO_EACH))
        assert text.count(" passes, ") == len(pairs)
        for outer, inner in pairs:
            assert f"| {inner} passes, {outer} fails |" in text
        for name in reported:
            assert (f"| {name} fails |" in text) == (not compared[name])

    chain = len({kinds[name] for name in reported}) == len(reported)
    for outcomes in itertools.product(OUTCOMES, repeat=len(reported)):
        col = column(TWO_EACH, dict(zip(reported, outcomes, strict=True)))
        for record in col.values():
            if record["outcome"] == "pass":
                continue
            culprit, basis = alerts.attribute(record, col, topology)
            assert culprit["outcome"] in ("fail", "error")
            # The culprit is reached from the failing layer along compared pairs that fail.
            reached, frontier = {record["ingress"]}, [record["ingress"]]
            while frontier:
                for inner in compared[frontier.pop()]:
                    if col[inner]["outcome"] != "pass" and inner not in reached:
                        reached.add(inner)
                        frontier.append(inner)
            assert culprit["ingress"] in reached
            inner = compared[culprit["ingress"]]
            if inner:
                assert basis == f"fails at {culprit['ingress']}, passes at {', '.join(inner)}"
                assert all(col[name]["outcome"] == "pass" for name in inner)
            else:
                assert "the innermost layer that ran this case" in basis
            if chain:
                assert (culprit, basis) == _depth_reading(record, col)


# ------------------------------------------------------------------------- the recorded graph


def test_a_graph_round_trips_through_its_record():
    topology = PD.topology()
    record = json.loads(json.dumps(topology.as_record()))
    assert Topology.from_record(record) == topology


@pytest.mark.parametrize(
    "body",
    [
        None,
        "graph",
        {"order": ["a"]},
        {"order": ["a", "a"], "fronts": {}},
        {"order": ["a", "b"], "fronts": {"a": ["c"]}},
        {"order": ["a", "b"], "fronts": {"b": ["a"]}},
        {"order": ["a", "b"], "fronts": {"a": ["b"], "b": ["a"]}},
        {"order": ["a"], "fronts": {"a": "b"}},
    ],
)
def test_an_unreadable_recorded_graph_is_read_as_none(body):
    """None means the reader falls back to depth; a bad graph never fails or hangs a reader."""
    assert Topology.from_record(body) is None


@pytest.mark.parametrize(
    "recorded",
    [
        {"order": [], "fronts": {}},
        {"order": ["router", "engine"], "fronts": {"router": ["engine"]}},
        {"order": ["gateway"], "fronts": {}},
    ],
)
def test_a_recorded_graph_that_misses_a_reported_layer_is_read_as_none(tmp_path, recorded):
    """Such a graph does not describe this run, so the feed is the depth reading."""
    profile = stack(("gateway", KINDS[0]), ("router", KINDS[1]), ("engine", KINDS[2]))
    outcomes = {"gateway": "fail", "router": "fail", "engine": "pass"}
    records = [{**r, "case_id": "c", "variant": "chat"} for r in column(profile, outcomes).values()]
    with_graph = alerts.build(run_dir(tmp_path, records, recorded))
    without = alerts.build(run_dir(tmp_path, records, None))
    assert json.dumps(with_graph) == json.dumps(without)
    (gateway,) = [r for r in with_graph["results"] if r["ingress"] == "gateway"]
    assert gateway["basis"] == "fails at router, passes at engine"


# ----------------------------------------------------------------------------- graph properties


def random_dag(rng: random.Random) -> Topology:
    names = [f"n{i}" for i in range(rng.randint(1, 7))]
    fronts = {
        name: tuple(other for other in names[i + 1 :] if rng.random() < 0.4)
        for i, name in enumerate(names)
    }
    return Topology(order=tuple(names), fronts=fronts)


def steps(topology: Topology, start: str, reported: set[str]) -> dict[str, int]:
    """Fewest edges from `start` to each layer, walking on only through layers not reported."""
    found: dict[str, int] = {}
    level, distance = list(topology.fronts[start]), 1
    while level:
        fresh = [name for name in level if name not in found]
        found.update(dict.fromkeys(fresh, distance))
        level = [t for name in fresh if name not in reported for t in topology.fronts[name]]
        distance += 1
    return found


@pytest.mark.parametrize("seed", range(200))
def test_random_graphs_keep_the_pairing_rule(seed):
    rng = random.Random(seed)
    topology = random_dag(rng)
    reported = {name for name in topology.order if rng.random() < 0.7}
    position = {name: i for i, name in enumerate(topology.order)}
    for outer, inner in topology.pairs(reported):
        assert position[outer] < position[inner]
    for name in reported:
        # The reported layers the fewest steps away, and none further once one is found.
        away = {n: d for n, d in steps(topology, name, reported).items() if n in reported}
        closest = {n for n, d in away.items() if d == min(away.values())}
        assert set(topology.nearest(name, reported)) == closest

    col = {name: {"ingress": name, "outcome": rng.choice(OUTCOMES)} for name in sorted(reported)}
    for record in col.values():
        if record["outcome"] == "pass":
            continue
        culprit, basis = alerts.attribute(record, col, topology)
        assert culprit["outcome"] in ("fail", "error")
        inner = topology.nearest(culprit["ingress"], col)
        assert all(col[name]["outcome"] == "pass" for name in inner)
        assert ("passes at" in basis) == bool(inner)


@pytest.mark.parametrize("seed", range(100))
def test_a_derived_order_is_topological_and_peers_have_no_edge(seed):
    rng = random.Random(seed)
    layers = [(f"n{i}", rng.randint(0, 3)) for i in range(rng.randint(1, 7))]
    topology = Topology.derive(layers)
    depths = dict(layers)
    assert list(topology.order) == [name for name, _ in sorted(layers, key=lambda x: x[1])]
    present = sorted(set(depths.values()))
    for name, targets in topology.fronts.items():
        later = [d for d in present if d > depths[name]]
        want = [n for n in topology.order if later and depths[n] == later[0]]
        assert list(targets) == want


# ------------------------------------------------------------------------ the declared graph

# The P/D stack of profiles/REFERENCE.md, declared in an order that is not topological.
DECLARED_PD = (
    ("decode", ()),
    ("epp", ("sidecar",)),
    ("prefill", ()),
    ("sidecar", ("prefill", "decode")),
)


def test_a_declared_graph_is_ordered_topologically_with_ties_in_declaration_order():
    topology = Topology.declare(DECLARED_PD)
    assert topology.order == ("epp", "sidecar", "decode", "prefill")
    assert dict(topology.fronts) == dict(DECLARED_PD)


def test_a_declared_graph_compares_along_its_edges_only():
    topology = Topology.declare(DECLARED_PD)
    assert topology.pairs(["epp", "sidecar", "prefill", "decode"]) == [
        ("epp", "sidecar"),
        ("sidecar", "decode"),
        ("sidecar", "prefill"),
    ]
    # Without the sidecar reporting, the router is compared with both engines behind it.
    assert topology.pairs(["epp", "prefill", "decode"]) == [("epp", "decode"), ("epp", "prefill")]


def test_behind_is_everything_reachable_in_order():
    topology = Topology.declare(DECLARED_PD)
    assert topology.behind("epp") == ("sidecar", "decode", "prefill")
    assert topology.behind("sidecar") == ("decode", "prefill")
    assert topology.behind("decode") == ()
    assert topology.behind("unknown") == ()
    assert Topology.derive([("gw", 0), ("r", 1), ("e", 2)]).behind("gw") == ("r", "e")


@pytest.mark.parametrize(
    "layers, problem",
    [
        (
            [("epp", ("sidcar",)), ("sidecar", ())],
            "ingresses[0](epp).fronts[0]: no ingress 'sidcar'",
        ),
        ([("epp", ("epp",)), ("e", ())], "ingresses[0](epp).fronts: lists itself"),
        (
            [("gw", ("epp",)), ("epp", ("sidecar",)), ("sidecar", ("epp",))],
            "ingresses: fronts form a cycle: epp -> sidecar -> epp",
        ),
        (
            [("epp", ("decode",)), ("decode", ()), ("prefill", ())],
            "ingresses[2](prefill): on no edge; it fronts nothing and nothing fronts it",
        ),
        # Every bad entry is named; the cycle and the loner wait until the entries are good.
        (
            [("a", ("b",)), ("b", ("a", "x")), ("c", ())],
            "ingresses[1](b).fronts[1]: no ingress 'x'",
        ),
    ],
)
def test_a_declared_graph_that_is_not_one_names_its_problem(layers, problem):
    with pytest.raises(loader.ProfileError) as caught:
        Topology.declare(layers)
    assert caught.value.problems == (problem,)


def test_every_bad_fronts_entry_is_named_in_one_error():
    layers = [("gw", ("rtr", "gw")), ("rtr", ("e", "e", "ghost")), ("e", ())]
    with pytest.raises(loader.ProfileError) as caught:
        Topology.declare(layers)
    assert caught.value.problems == (
        "ingresses[0](gw).fronts: lists itself",
        "ingresses[1](rtr).fronts[1]: lists 'e' twice",
        "ingresses[1](rtr).fronts[2]: no ingress 'ghost'",
    )


def test_every_graph_problem_is_named_once_the_entries_are_good():
    layers = [("a", ("b",)), ("b", ("a",)), ("c", ("d",)), ("d", ("c",)), ("e", ())]
    with pytest.raises(loader.ProfileError) as caught:
        Topology.declare(layers)
    assert caught.value.problems == (
        "ingresses: fronts form a cycle: a -> b -> a",
        "ingresses[4](e): on no edge; it fronts nothing and nothing fronts it",
    )


@pytest.mark.parametrize(
    "outer, inner",
    [("engine", "router"), ("engine", "gateway"), ("router", "gateway")],
)
def test_an_edge_against_the_positions_is_refused(outer, inner):
    depths = {"gateway": 0, "router": 1, "engine": 2}
    layers = [(outer, (inner,)), (inner, ())]
    with pytest.raises(loader.ProfileError) as caught:
        Topology.declare(layers, {outer: depths[outer], inner: depths[inner]})
    assert caught.value.problems == (
        f"ingresses[0]({outer}).fronts[0]: {inner!r} is at a shallower position than "
        f"{outer!r}; an ingress fronts only ingresses at its own position or deeper",
    )


def test_an_edge_between_ingresses_at_one_position_is_allowed():
    """A router in front of a routing sidecar, both routers, is a chain, not a contradiction."""
    layers = [("epp", ("sidecar",)), ("sidecar", ("decode",)), ("decode", ())]
    topology = Topology.declare(layers, {"epp": 1, "sidecar": 1, "decode": 2})
    assert topology.order == ("epp", "sidecar", "decode")


def test_one_ingress_declared_as_fronting_nothing_is_a_graph():
    assert Topology.declare([("only", ())]).order == ("only",)


def test_a_declared_graph_round_trips_through_its_record():
    topology = Topology.declare(DECLARED_PD)
    record = json.loads(json.dumps(topology.as_record()))
    assert Topology.from_record(record) == topology


@pytest.mark.parametrize(
    "layers",
    [
        [("a", ("b",)), ("a", ("b",)), ("b", ())],
        [("a", ("b",)), ("b", ()), ("a", ())],
    ],
)
def test_a_duplicate_id_counts_once_with_its_first_declaration(layers):
    """The loader refuses a duplicate id; a graph built in code still orders, never hangs."""
    topology = Topology.declare(layers)
    assert topology.order == ("a", "b")
    assert dict(topology.fronts) == {"a": ("b",), "b": ()}


def test_an_id_listed_twice_in_one_fronts_is_refused():
    with pytest.raises(loader.ProfileError) as caught:
        Topology.declare([("a", ("b", "c", "b")), ("b", ()), ("c", ())])
    assert caught.value.problems == ("ingresses[0](a).fronts[2]: lists 'b' twice",)


def random_declared(rng: random.Random) -> list[tuple[str, tuple[str, ...]]]:
    """A connected acyclic declaration in a shuffled order: every ingress but the first in
    rank is fronted by one ranked before it, and some get further edges forward in rank."""
    ranked = [f"n{i}" for i in range(rng.randint(2, 7))]
    edges: dict[str, list[str]] = {name: [] for name in ranked}
    for index, name in enumerate(ranked[1:], start=1):
        edges[rng.choice(ranked[:index])].append(name)
    for index, name in enumerate(ranked):
        for later in ranked[index + 1 :]:
            if later not in edges[name] and rng.random() < 0.3:
                edges[name].append(later)
    declared = [(name, tuple(targets)) for name, targets in edges.items()]
    rng.shuffle(declared)
    return declared


@pytest.mark.parametrize("seed", range(100))
def test_a_declared_order_puts_every_edge_later(seed):
    layers = random_declared(random.Random(seed))
    topology = Topology.declare(layers)
    position = {name: index for index, name in enumerate(topology.order)}
    assert all(position[t] > position[n] for n, targets in layers for t in targets)
    assert Topology.from_record(topology.as_record()) == topology


@pytest.mark.parametrize("seed", range(100))
def test_a_back_edge_is_reported_as_a_cycle_along_real_edges(seed):
    rng = random.Random(seed)
    layers = random_declared(rng)
    topology = Topology.declare(layers)
    start = rng.choice([name for name in topology.order if topology.behind(name)])
    back = rng.choice(topology.behind(start))
    fronts = dict(layers)
    fronts[back] = (*fronts[back], start)
    with pytest.raises(loader.ProfileError) as caught:
        Topology.declare(fronts.items())
    (problem,) = caught.value.problems
    assert problem.startswith("ingresses: fronts form a cycle: ")
    path = problem.removeprefix("ingresses: fronts form a cycle: ").split(" -> ")
    assert len(path) >= 3 and path[0] == path[-1]
    assert all(inner in fronts[outer] for outer, inner in itertools.pairwise(path))


def _cycle_recursive(names, fronts):
    """The cycle search as first written, recursively: the oracle for the iterative one."""
    done, path = set(), []

    def walk(name):
        if name in path:
            return [*path[path.index(name) :], name]
        if name in done:
            return []
        path.append(name)
        for target in fronts.get(name, ()):
            found = walk(target)
            if found:
                return found
        path.pop()
        done.add(name)
        return []

    for name in names:
        found = walk(name)
        if found:
            return found
    return []


@pytest.mark.parametrize("seed", range(300))
def test_the_iterative_cycle_search_finds_what_the_recursive_one_did(seed):
    from llmdv.profile.topology import _cycle

    rng = random.Random(seed)
    names = [f"n{i}" for i in range(rng.randint(1, 9))]
    fronts = {
        name: tuple(dict.fromkeys(rng.sample(names, rng.randint(0, min(3, len(names))))))
        for name in names
    }
    assert _cycle(names, fronts) == _cycle_recursive(names, fronts)


def test_a_very_long_chain_is_ordered_and_a_cycle_at_its_end_is_named():
    """No recursion limit: a chain of any length is checked as a profile, never a crash."""
    depth_ = 5000
    chain = [(f"i{k}", (f"i{k + 1}",) if k < depth_ - 1 else ()) for k in range(depth_)]
    assert Topology.declare(chain).order[-1] == f"i{depth_ - 1}"
    looped = chain[:-1] + [(f"i{depth_ - 1}", ("i0",))]
    with pytest.raises(loader.ProfileError, match="fronts form a cycle: i0 -> i1"):
        Topology.declare(looped)
