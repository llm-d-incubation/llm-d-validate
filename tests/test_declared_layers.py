"""A profile that declares which layer fronts which, run end to end against a fake fleet.

The stack is the P/D one depth cannot describe: a router in front of the decode pod's routing
sidecar, which forwards to a prefill and a decode engine. The run's summary and its alert feed
must read the declared edges: a defect at the sidecar points at the sidecar, a defect at the
decode engine points at the decode engine, and prefill and decode, which are peers, are never
compared with each other.
"""

from __future__ import annotations

import contextlib

import yaml

from llmdv.evidence import alerts, dashboard
from tests.fakes import FakeFleet, only_run, profile_yaml, run_llmdv
from tests.test_dashboard import payload

LAYERS = (
    ("epp", "router", ["sidecar"]),
    ("sidecar", "router", ["prefill", "decode"]),
    ("prefill", "engine", None),
    ("decode", "engine", None),
)


def run_pd(tmp_path, broken: set[str], declare: bool = True):
    """Run one case against four fakes, `broken` of them aborting a generation on a 200."""
    with contextlib.ExitStack() as stack:
        fleets = {
            name: stack.enter_context(FakeFleet(abort_finish_reason=name in broken))
            for name, _, _ in LAYERS
        }
        first = next(iter(fleets.values()))
        raw = yaml.safe_load(profile_yaml(first.base_url, first.model))
        (template,) = raw["ingresses"]
        raw["ingresses"] = []
        for name, kind, fronts in LAYERS:
            ingress = {**template, "id": name, "kind": kind, "base_url": fleets[name].base_url}
            if declare and fronts is not None:
                ingress["fronts"] = fronts
            raw["ingresses"].append(ingress)
        raw["headers"] = {}
        profile = tmp_path / "pd.yaml"
        profile.write_text(yaml.safe_dump(raw, sort_keys=False))
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify", "--profile", str(profile), "--local-output", str(run_dir),
            "--pod-logs", "off", "-k", "test_chat_basic and not sdk",
        )  # fmt: skip
        run_dir = only_run(run_dir)
    assert result.returncode == 1, result.stderr
    return run_dir


def failing_points(run_dir) -> dict[str, tuple[str, str]]:
    """Each failing ingress to where the feed points and why."""
    feed = alerts.build(run_dir)
    return {
        item["ingress"]: (item["points_at"], item["basis"])
        for item in feed["results"]
        if item["outcome"] in ("fail", "error")
    }


def assert_no_peer_row(section: str) -> None:
    assert "decode passes, prefill fails" not in section
    assert "prefill passes, decode fails" not in section
    assert "prefill -> decode" not in section and "decode -> prefill" not in section


def attribution(run_dir) -> str:
    summary = (run_dir / "summary.md").read_text()
    return summary[summary.index("## attribution") :]


def test_a_defect_at_the_sidecar_points_at_the_sidecar(tmp_path):
    run_dir = run_pd(tmp_path, {"epp", "sidecar"})
    points = failing_points(run_dir)
    assert set(points) == {"epp", "sidecar"}
    for points_at, basis in points.values():
        assert points_at == "sidecar"
        assert basis == "fails at sidecar, passes at prefill, decode"
    section = attribution(run_dir)
    assert "compared along: epp -> sidecar, sidecar -> prefill, sidecar -> decode" in section
    assert "| sidecar passes, epp fails |" in section
    assert "| prefill passes, sidecar fails |" in section
    assert "| decode passes, sidecar fails |" in section
    assert_no_peer_row(section)


def test_a_defect_at_decode_points_at_decode_and_never_compares_it_with_prefill(tmp_path):
    run_dir = run_pd(tmp_path, {"epp", "sidecar", "decode"})
    points = failing_points(run_dir)
    assert set(points) == {"epp", "sidecar", "decode"}
    assert {points_at for points_at, _ in points.values()} == {"decode"}
    # decode fronts nothing, so passing prefill is no evidence about it.
    assert "prefill" not in points["decode"][1]
    assert_no_peer_row(attribution(run_dir))
    # The dashboard shows each card's declared edges.
    data = payload(dashboard.write(run_dir))
    assert data["fronts"] == {
        "epp": ["sidecar"],
        "sidecar": ["prefill", "decode"],
        "prefill": [],
        "decode": [],
    }
    # The page draws one card per profile ingress and looks its row up by the card's id.
    cards = [ingress["id"] for ingress in data["profile"]["ingresses"]]
    assert cards == list(data["fronts"])


def test_without_declared_edges_the_same_defect_is_read_by_depth(tmp_path):
    """The control: by depth the two routers are peers, so the router's failure is read as
    its own and the sidecar's as the sidecar's. The declared edges are what put one in front
    of the other."""
    run_dir = run_pd(tmp_path, {"epp", "sidecar"}, declare=False)
    points = {name: at for name, (at, _) in failing_points(run_dir).items()}
    assert points == {"epp": "epp", "sidecar": "sidecar"}
    assert "layers of one kind are peers" in attribution(run_dir)
    assert payload(dashboard.write(run_dir))["fronts"] is None


def test_profile_show_prints_each_ingress_s_declared_fronts(tmp_path):
    """The declared shape is visible before a run, not only on the run's dashboard."""
    path = tmp_path / "pd.yaml"
    path.write_text(
        "schema: llmdv.profile/1\nname: pd\nmodel: {public_id: m}\ningresses:\n"
        "  - {id: epp, kind: router, base_url: 'http://localhost:1', fronts: [sidecar]}\n"
        "  - {id: sidecar, kind: router, base_url: 'http://localhost:2',"
        " fronts: [prefill, decode]}\n"
        "  - {id: prefill, kind: engine, base_url: 'http://localhost:3'}\n"
        "  - {id: decode, kind: engine, base_url: 'http://localhost:4'}\n"
    )
    shown = run_llmdv("profile", "show", str(path))
    assert shown.returncode == 0, shown.stderr
    assert "fronts=sidecar" in shown.stdout
    assert "fronts=prefill, decode" in shown.stdout
    assert shown.stdout.count("fronts=nothing") == 2
