"""Long-context cases, each proven to fail on the defect it targets."""

import dataclasses
import json

import pytest
import yaml

from llmdv.profile.schema import Capabilities, Ingress, IngressKind, Model, Timeouts
from llmdv.suites.long_context.test_window import OUTPUT_OVERFLOW_TIMEOUT_S, output_overflow_timeout
from tests.fakes import FakeFleet
from tests.test_cache_reuse import BLOCKS, RATIO
from tests.test_suites_against_fake import run_suites, write_profile

SURFACES = ["chat_completions", "messages", "responses"]
GENERATION = [*SURFACES, "completions"]
WINDOW = 1024


def run_case(
    tmp_path,
    select,
    surface,
    *,
    fleet_kw=None,
    cache=None,
    checks=(),
    policy=None,
    error_status=None,
    rate_limited=False,
    request_timeout=None,
    stream_idle=None,
):
    with FakeFleet(max_model_len=WINDOW, **(fleet_kw or {})) as fleet:
        path = write_profile(
            tmp_path,
            fleet,
            responses=True,
            max_model_len=WINDOW,
            fleet_policy=policy,
            rate_limited=rate_limited,
        )
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = [surface]
        raw["capabilities"] = {"scenario_checks": list(checks)}
        if error_status:
            raw["ingresses"][0].setdefault("error_status", {}).update(error_status)
        timeouts = {"request": request_timeout, "stream_idle": stream_idle}
        timeouts = {name: value for name, value in timeouts.items() if value is not None}
        if timeouts:
            raw["ingresses"][0]["timeouts"] = timeouts
        if cache is not None:
            raw["capabilities"]["cache_reuse"] = {
                "surfaces": [surface],
                "prefix_words": 128,
                **cache,
            }
        path.write_text(yaml.safe_dump(raw))
        return run_suites(path, tmp_path / "run", "-k", select, "--llmdv-surface", surface)


@pytest.mark.parametrize("surface", GENERATION)
@pytest.mark.parametrize(
    "declared,refuse,outcome",
    [
        # Undeclared: the engine must refuse, naming the window.
        (None, True, "pass"),
        (None, False, "fail"),
        # Declared served (vLLM since #57035): a generation within the window passes, a refusal
        # fails.
        ([200], False, "pass"),
        ([200], True, "fail"),
    ],
)
def test_output_overflow_follows_the_declared_contract(
    tmp_path, surface, declared, refuse, outcome
):
    code, records = run_case(
        tmp_path,
        "test_output_budget_over_the_window",
        surface,
        fleet_kw={"refuse_output_overflow": refuse},
        checks=("output_overflow",),
        error_status={"output_overflow": declared} if declared else None,
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records
    if outcome == "fail":
        failed = [c["name"] for c in records[0]["checks"] if not c["ok"]]
        assert failed and failed[0] in ("output_overflow_generation", "output_overflow_clamp"), (
            records
        )


def test_output_overflow_fails_a_generation_that_outlasts_its_timeout(tmp_path):
    # A clamped budget the model never ends: the case gives up at its timeout and fails, rather
    # than waiting out the ingress's whole request timeout.
    code, records = run_case(
        tmp_path,
        "test_output_budget_over_the_window",
        "chat_completions",
        fleet_kw={"delay_s": 2.0},
        checks=("output_overflow",),
        error_status={"output_overflow": [200]},
        request_timeout=0.5,
    )
    assert code == 1, records
    assert records[0]["outcome"] == "fail", records
    failed = [c["name"] for c in records[0]["checks"] if not c["ok"]]
    assert failed[:1] == ["output_overflow_generation"], records
    assert "transport error" in records[0]["reason"], records


INGRESS = Ingress(
    id="test",
    kind=IngressKind.VLLM,
    base_url="http://unused",
    model=Model(public_id="m"),
    capabilities=Capabilities(),
)


@pytest.mark.parametrize(
    "request_timeout,expected",
    [(600.0, OUTPUT_OVERFLOW_TIMEOUT_S), (30.0, 30.0)],
)
def test_output_overflow_timeout_caps_the_ingress_timeout(request_timeout, expected):
    ingress = dataclasses.replace(INGRESS, timeouts=Timeouts(request=request_timeout))
    assert output_overflow_timeout(ingress) == expected


LONG = ("long_context",)


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("select", ["test_near_limit_prompt", "test_long_session"])
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (BLOCKS, None, "pass"),
        (RATIO, None, "pass"),
        # Long prompts are placed without prefix data, while the 128-word probe still hits.
        (BLOCKS, "long_prompt_blind", "fail"),
        (BLOCKS, "render_with_budget", "fail"),
    ],
)
def test_long_prompts_stay_warm(tmp_path, surface, select, declaration, defect, outcome):
    code, records = run_case(
        tmp_path,
        select,
        surface,
        fleet_kw={"cache_probe": True, "cache_defect": defect},
        cache=declaration,
        checks=LONG,
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "gap_s,outcome",
    [
        # Each frame of a long reply follows the last within the idle bound, and the whole reply
        # takes longer than the request timeout: a live generation, which a non-streamed request
        # would fail as a read timeout.
        (0.2, "pass"),
        # A stream that stops sending for longer than the bound still fails.
        (1.5, "fail"),
    ],
)
def test_near_limit_streams_a_reply_longer_than_the_request_timeout(
    tmp_path, surface, gap_s, outcome
):
    code, records = run_case(
        tmp_path,
        "test_near_limit_prompt",
        surface,
        fleet_kw={"cache_probe": True, "long_decode_gap_s": gap_s},
        cache=BLOCKS,
        checks=LONG,
        request_timeout=0.5,
        stream_idle=0.5,
    )
    (record,) = records
    assert code == (1 if outcome == "fail" else 0), record
    assert record["outcome"] == outcome, record
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    calibration, *measured = [json.loads(line) for line in lines]
    assert "stream" not in calibration["request_body"]
    assert measured and all(e["request_body"]["stream"] is True for e in measured), measured
    if outcome == "pass":
        assert all(e["elapsed_s"] > 0.5 for e in measured), [e["elapsed_s"] for e in measured]
    else:
        assert measured[-1]["transport_error"], measured[-1]


def test_long_prompt_blind_passes_the_short_probe(tmp_path):
    with FakeFleet(cache_probe=True, cache_defect="long_prompt_blind") as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"] = {"cache_reuse": {**BLOCKS, "prefix_words": 128}}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_native_cache_reuse")
    assert code == 0 and records[0]["outcome"] == "pass", records


@pytest.mark.parametrize("select", ["test_near_limit_prompt", "test_long_session"])
def test_long_prompts_are_excluded_on_shared_fleets(tmp_path, select):
    code, records = run_case(
        tmp_path,
        select,
        "chat_completions",
        fleet_kw={"cache_probe": True},
        cache=BLOCKS,
        checks=LONG,
        policy="shared",
    )
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "[excluded_by_policy]" in records[0]["reason"], records


@pytest.mark.parametrize("declared", [None, [200]])
def test_output_overflow_under_a_declared_quota_skips_as_throttled(tmp_path, declared):
    """A 429 from a rate-limited ingress is the quota working, in either declared direction."""
    code, records = run_case(
        tmp_path,
        "test_output_budget_over_the_window",
        "chat_completions",
        fleet_kw={"throttle": True},
        checks=("output_overflow",),
        error_status={"output_overflow": declared} if declared else None,
        rate_limited=True,
    )
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert records[0]["skip_class"] == "throttled", records


def test_output_overflow_skips_undeclared(tmp_path):
    code, records = run_case(tmp_path, "test_output_budget_over_the_window", "chat_completions")
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "[undeclared]" in records[0]["reason"], records
