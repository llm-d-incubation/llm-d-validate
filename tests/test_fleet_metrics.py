"""Metrics cases against a fake Prometheus HTTP API: each defect fails its own rule.

The fake is a real server on a local socket answering GET /api/v1/query from a series table.
It refuses every other method and path and records each request, so the tests also prove the
client only ever reads.
"""

from __future__ import annotations

import json

import pytest

from llmdv import preflight
from llmdv.fleet.prometheus import MetricsError, PrometheusReader
from llmdv.profile import loader
from llmdv.profile.fleet import MetricsSource
from tests.fleet_fakes import (
    FLEET_METRICS_YAML,
    HEADER_LABEL_DEFECTS,
    METRIC_DEFECTS,
    FakeObjectiveRouter,
    FakePrometheus,
    fleet_profile,
    header_label_profile,
    metric_series,
    metrics_environment,
    run_fleet_suite,
    verdicts,
)

METRIC_CASES = ("metrics.bounds", "metrics.agree", "metrics.spread")
TOKEN = "metrics-token-1234"


def run(tmp_path, url, blocks=None, env=None):
    blocks = blocks if blocks is not None else (metrics_environment(url), FLEET_METRICS_YAML)
    profile = fleet_profile(tmp_path / "profile.yaml", *blocks)
    return run_fleet_suite(
        profile, tmp_path / "run", {"PATH": "/usr/bin:/bin", **(env or {})}, "-k", "metrics"
    )


def rows(records):
    return {k: v for k, v in verdicts(records).items() if k[0] in METRIC_CASES}


def test_a_healthy_fleet_passes_every_rule(tmp_path):
    with FakePrometheus(metric_series()) as prom:
        result, records = run(tmp_path, prom.base_url)
    found = rows(records)
    assert set(found.values()) == {"pass"}, (found, result.stdout[-3000:])
    assert {variant for _, variant in found} == {t[1] for t in METRIC_DEFECTS.values()}
    assert {(method, path) for method, path, _ in prom.requests} == {("GET", "/api/v1/query")}


@pytest.mark.parametrize("defect", sorted(METRIC_DEFECTS))
def test_each_defect_fails_its_own_rule(tmp_path, defect):
    with FakePrometheus(metric_series((defect,))) as prom:
        result, records = run(tmp_path, prom.base_url)
    found = rows(records)
    target = METRIC_DEFECTS[defect][:2]
    assert found[target] == "fail", (defect, found, result.stdout[-3000:])
    assert {v for k, v in found.items() if k != target} == {"pass"}, (defect, found)


def test_an_absent_counter_reads_as_zero_only_by_declaration(tmp_path):
    """`kv_events_skipped` passes with no series; `kv_index_moving` fails with none."""
    with FakePrometheus(metric_series(("series_missing",))) as prom:
        _, records = run(tmp_path, prom.base_url)
    found = rows(records)
    assert found[("metrics.bounds", "kv_events_skipped")] == "pass"
    assert found[("metrics.bounds", "kv_index_moving")] == "fail"


def test_without_a_source_every_case_is_undeclared(tmp_path):
    _, records = run(tmp_path, "", blocks=())
    found = [r for r in records if r.get("case_id") in METRIC_CASES]
    assert sorted(r["case_id"] for r in found) == sorted(METRIC_CASES)
    assert {(r["outcome"], r["skip_class"]) for r in found} == {("skip", "undeclared")}


def test_a_dead_source_is_missing_instrumentation(tmp_path):
    with FakePrometheus({}) as prom:
        url = prom.base_url
    _, records = run(tmp_path, url)
    found = [r for r in records if r.get("case_id") in METRIC_CASES]
    assert found and {r["skip_class"] for r in found} == {"requires_instrumentation"}


def test_a_rejected_query_fails(tmp_path):
    block = "fleet:\n  metrics:\n    - {id: bad, check: bounds, query: 'BAD(x)', max: 1}\n"
    with FakePrometheus({}) as prom:
        _, records = run(
            tmp_path, prom.base_url, blocks=(metrics_environment(prom.base_url), block)
        )
    row = next(r for r in records if r.get("variant") == "bad")
    assert row["outcome"] == "fail" and "bad_data" in row["reason"]


def test_the_bearer_token_is_sent_and_never_recorded(tmp_path):
    blocks = (metrics_environment("{url}", key_env="LLMDV_TEST_METRICS_TOKEN"), FLEET_METRICS_YAML)
    with FakePrometheus(metric_series(), token=TOKEN) as prom:
        blocks = tuple(block.replace("{url}", prom.base_url) for block in blocks)
        _, records = run(tmp_path, prom.base_url, blocks, {"LLMDV_TEST_METRICS_TOKEN": TOKEN})
        _, denied = run(tmp_path / "denied", prom.base_url, blocks)
    assert set(rows(records).values()) == {"pass"}
    assert {r["skip_class"] for r in denied if r.get("case_id") in METRIC_CASES} == {
        "requires_instrumentation"
    }
    for path in (tmp_path / "run").rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(errors="replace"), path


# ----------------------------------------------------------------- preflight


def profile_for(tmp_path, url, key_env=None):
    return loader.load(fleet_profile(tmp_path / "p.yaml", metrics_environment(url, key_env)))


def test_preflight_accepts_a_live_source(tmp_path):
    with FakePrometheus({}) as prom:
        report = preflight.Report()
        preflight._check_metrics(report, profile_for(tmp_path, prom.base_url))
    assert report.ok
    assert prom.requests == [("GET", "/api/v1/query", "vector(1)")]


def test_preflight_fails_a_dead_source_as_the_environment(tmp_path):
    with FakePrometheus({}) as prom:
        url = prom.base_url
    report = preflight.Report()
    preflight._check_metrics(report, profile_for(tmp_path, url))
    assert [p.kind for p in report.problems] == ["environment"]


def test_preflight_fails_a_missing_token_as_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMDV_TEST_METRICS_TOKEN", raising=False)
    report = preflight.Report()
    preflight._check_metrics(
        report, profile_for(tmp_path, "http://127.0.0.1:9", "LLMDV_TEST_METRICS_TOKEN")
    )
    assert [p.kind for p in report.problems] == ["credentials"]


def test_preflight_ignores_a_profile_without_a_source(tmp_path):
    report = preflight.Report()
    preflight._check_metrics(report, loader.load(fleet_profile(tmp_path / "p.yaml")))
    assert report.ok


# ----------------------------------------------------------------- client and declarations


def test_the_client_reads_vectors_and_classifies_errors():
    series = {"up": [({"job": "a"}, 1), ({"job": "b"}, "NaN")]}
    with FakePrometheus(series) as prom:
        reader = PrometheusReader(MetricsSource(url=prom.base_url))
        samples = reader.query("up")
        with pytest.raises(MetricsError) as rejected:
            reader.query("BAD(")
    assert samples[0].labels == {"job": "a"} and samples[0].value == 1
    assert samples[1].value != samples[1].value  # NaN
    assert rejected.value.kind == "rejected"


@pytest.mark.parametrize(
    "block, message",
    [
        ("fleet:\n  metrics:\n    - {id: a, check: bounds, query: q}\n", "min, max"),
        ("fleet:\n  metrics:\n    - {id: a, check: bounds, query: q, min: 2, max: 1}\n", "above"),
        (
            "fleet:\n  metrics:\n    - {id: a, check: agree, left: l, right: r}\n",
            "exactly one",
        ),
        (
            "fleet:\n  metrics:\n    - {id: a, check: spread, query: q, min_ratio: 2}\n",
            "min_ratio",
        ),
        ("fleet:\n  metrics:\n    - {id: a, check: nope}\n", "check"),
        ("environment:\n  metrics: {url: localhost:9090}\n", "http"),
        ("environment:\n  metrics: {url: 'http://x', auth: {type: bearer}}\n", "key_env"),
        (
            "fleet:\n  metrics:\n    - {id: a, check: header_label, ingress: fake,"
            " query: q, headers: {x: y}, label: l, expect: e, interval_s: 0}\n",
            "positive",
        ),
    ],
)
def test_malformed_metrics_declarations(tmp_path, block, message):
    with pytest.raises(loader.ProfileError, match=message):
        loader.load(fleet_profile(tmp_path / "p.yaml", block))


def test_rules_without_a_source_are_a_validation_error(tmp_path):
    profile = loader.load(fleet_profile(tmp_path / "p.yaml", FLEET_METRICS_YAML))
    assert any("environment.metrics is unset" in str(d) for d in loader.validate(profile))


def test_metrics_results_are_json_lines(tmp_path):
    """The journaled series stay serialisable, so results.jsonl and compare can read them."""
    with FakePrometheus(metric_series()) as prom:
        _, records = run(tmp_path, prom.base_url)
    assert records and all(json.dumps(r) for r in records)


# ----------------------------------------------------------------- header -> label


def header_run(tmp_path, defect=None):
    team = "team-a" if defect != "identity_lost" else "default"
    with FakeObjectiveRouter(defect, default_fairness=team, default_priority="60") as router:
        with FakePrometheus({}, answer=router.answer) as prom:
            profile = header_label_profile(tmp_path / "p.yaml", router.base_url, prom.base_url)
            return run_fleet_suite(
                profile, tmp_path / "run", {"PATH": "/usr/bin:/bin"}, "-k", "header_label"
            )


def label_rows(records):
    return {
        r["variant"]: r["outcome"] for r in records if r.get("case_id") == "metrics.header_label"
    }


def test_declared_headers_land_on_their_label(tmp_path):
    result, records = header_run(tmp_path)
    found = label_rows(records)
    assert found == dict.fromkeys(
        ("objective_premium", "objective_unknown", "objective_alternate_spelling", "team_identity"),
        "pass",
    ), (found, result.stdout[-3000:])


@pytest.mark.parametrize("defect", sorted(HEADER_LABEL_DEFECTS))
def test_each_header_defect_fails_its_own_rule(tmp_path, defect):
    result, records = header_run(tmp_path, defect)
    found = label_rows(records)
    target = HEADER_LABEL_DEFECTS[defect]
    assert found[target] == "fail", (defect, found, result.stdout[-3000:])
    # An ignored objective also breaks the alternate spelling, which names one.
    spared = {"objective_alternate_spelling"} if defect == "objective_ignored" else set()
    others = {k: v for k, v in found.items() if k != target and k not in spared}
    assert set(others.values()) == {"pass"}, (defect, found)


def test_a_header_rule_sends_one_request_and_no_engine_internal_fields(tmp_path):
    """The probe is one ordinary request: no admin route, no engine-internal fields."""
    _, records = header_run(tmp_path)
    exchanges = [
        json.loads(line)
        for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(exchanges) == 4
    for exchange in exchanges:
        text = json.dumps(exchange)
        for field in ("vllm_xargs", "kv_transfer_params", 'priority"', "kv_cache_report_mode"):
            assert field not in text


def test_a_non_nonce_counter_cannot_pass_with_nan_growth(tmp_path):
    with FakeObjectiveRouter(default_fairness="team-a", default_priority="60") as router:

        def answer(expr):
            found = router.answer(expr)
            if 'fairness_id="team-a"' in expr:
                return [({"priority": "60"}, "NaN")]
            return found

        with FakePrometheus({}, answer=answer) as prom:
            profile = header_label_profile(tmp_path / "p.yaml", router.base_url, prom.base_url)
            _, records = run_fleet_suite(
                profile, tmp_path / "run", {"PATH": "/usr/bin:/bin"}, "-k", "header_label"
            )
    assert label_rows(records)["team_identity"] == "fail"


def test_a_partial_answer_is_missing_instrumentation(tmp_path):
    """A querier with a store down answers with warnings; its data cannot prove anything."""
    warning = ["No StoreAPIs matched for this query"]
    with FakePrometheus(metric_series(), warnings=warning) as prom:
        _, records = run(tmp_path, prom.base_url)
    found = [r for r in records if r.get("case_id") in METRIC_CASES]
    assert found and {(r["outcome"], r["skip_class"]) for r in found} == {
        ("skip", "requires_instrumentation")
    }
    assert "partial answer" in found[0]["reason"]


@pytest.mark.parametrize("value", ["NaN", "+Inf"])
def test_spread_rejects_non_finite_values(tmp_path, value):
    series = metric_series()
    series["kv_blocks_by_pod"] = [
        ({"pod": f"p{index}", "blocks": value if index == 3 else 1000}, 1) for index in range(4)
    ]
    with FakePrometheus(series) as prom:
        _, records = run(tmp_path, prom.base_url)
    assert rows(records)[("metrics.spread", "kv_capacity_uniform")] == "fail"
