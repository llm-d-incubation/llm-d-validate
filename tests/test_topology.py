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

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = sorted(
    [p for p in (ROOT / "profiles").rglob("*.yaml") if not p.name.endswith(".local.yaml")]
    + [ROOT / "deploy" / "overlays" / "example" / "profile.yaml"]
)
OUTCOMES = ("pass", "fail", "error")
KINDS = (IngressKind.LITELLM, IngressKind.LLMD_ROUTER, IngressKind.VLLM)


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
                    want = alerts._attribute_by_depth(record, col)
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


def test_an_archived_run_without_a_graph_keeps_the_depth_reading(tmp_path):
    """Archived feeds are rebuilt byte for byte, so the depth walk's peer step is kept there.

    Known wrong, kept on purpose: with no graph recorded, the depth walk steps from the
    failing prefill to its passing peer. A run that records its graph never reads this way.
    """
    outcomes = {"epp": "fail", "prefill": "fail", "decode": "pass"}
    records = [{**r, "case_id": "c", "variant": "chat"} for r in column(PD, outcomes).values()]
    feed = alerts.build(run_dir(tmp_path, records, None))
    (epp,) = [r for r in feed["results"] if r["ingress"] == "epp"]
    assert epp["basis"] == "fails at prefill, passes at decode"


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


# ----------------------------------------------------------------------------- graph properties


def random_dag(rng: random.Random) -> Topology:
    names = [f"n{i}" for i in range(rng.randint(1, 7))]
    fronts = {
        name: tuple(other for other in names[i + 1 :] if rng.random() < 0.4)
        for i, name in enumerate(names)
    }
    return Topology(order=tuple(names), fronts=fronts)


def reachable(topology: Topology, start: str, avoid: set[str]) -> set[str]:
    found, stack_ = set(), list(topology.fronts[start])
    while stack_:
        current = stack_.pop()
        if current not in found:
            found.add(current)
            if current not in avoid:
                stack_.extend(topology.fronts[current])
    return found


@pytest.mark.parametrize("seed", range(200))
def test_random_graphs_keep_the_pairing_rule(seed):
    rng = random.Random(seed)
    topology = random_dag(rng)
    reported = {name for name in topology.order if rng.random() < 0.7}
    position = {name: i for i, name in enumerate(topology.order)}
    for outer, inner in topology.pairs(reported):
        assert position[outer] < position[inner]
        # A nearest reported descendant: reached without passing another reported layer.
        assert inner in reachable(topology, outer, reported)
    for name in reported:
        nearest = set(topology.nearest(name, reported))
        assert nearest == reachable(topology, name, reported) & reported

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
