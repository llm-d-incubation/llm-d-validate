"""Session reuse cases, each proven to fail on the defect it targets."""

import pytest
import yaml

from tests.fakes import FakeFleet
from tests.test_cache_repeats import SURFACES, TOOLS
from tests.test_cache_reuse import BLOCKS, RATIO, run_cache_case
from tests.test_suites_against_fake import run_suites, write_profile


def ran(records):
    return {r["variant"]: r["outcome"] for r in records if r["outcome"] != "skip"}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("model", [None, TOOLS])
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (BLOCKS, None, "pass"),
        (RATIO, None, "pass"),
        # Every turn that carries history reads nothing: a repeat case cannot see it.
        (BLOCKS, "history_blind", "fail"),
        (RATIO, "history_blind", "fail"),
    ],
)
def test_turn_reuse_reads_the_previous_turn(tmp_path, surface, model, declaration, defect, outcome):
    code, records = run_cache_case(
        tmp_path, surface, declaration, "test_turn_reuse", defect, model=model
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert set(ran(records).values()) == {outcome}, records
    assert len(ran(records)) == 2, records


def test_turn_reuse_passes_the_reply_back_natively(tmp_path):
    code, records = run_cache_case(tmp_path, "messages", BLOCKS, "test_turn_reuse", model=TOOLS)
    assert code == 0, records
    checks = {c["name"]: c["observed"] for c in records[0]["checks"]}
    assert checks["session_shape"] == {"tools": 2, "tool_exchange": True}, checks


INLINE = {"scenario_checks": ["inline_system"]}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "defect,outcome", [(None, "pass"), ("hoist_system", "fail"), ("merge_system", "fail")]
)
def test_inline_system_is_rendered_in_place(tmp_path, surface, defect, outcome):
    code, records = run_cache_case(
        tmp_path, surface, BLOCKS, "test_inline_system_reuse", defect, **INLINE
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


def test_inline_system_skips_undeclared(tmp_path):
    code, records = run_cache_case(tmp_path, "messages", BLOCKS, "test_inline_system_reuse")
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "[undeclared]" in records[0]["reason"], records


@pytest.mark.parametrize(
    "select,scenario",
    [
        ("test_inline_system_reuse", "inline_system"),
        ("test_cache_hints_reuse", "cache_hints"),
    ],
)
def test_scenario_cache_cases_skip_without_cache_reuse(tmp_path, select, scenario):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["messages"]
        raw["capabilities"] = {"scenario_checks": [scenario]}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path, tmp_path / "run", "-k", select, "--llmdv-surface", "messages"
        )
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "cache_reuse" in records[0]["reason"], records


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (BLOCKS, None, "pass"),
        (RATIO, None, "pass"),
        # The compacted session lands on a replica without the system prefix.
        (BLOCKS, "session_by_head", "fail"),
        (BLOCKS, "history_blind", "fail"),
    ],
)
def test_compaction_keeps_the_system_prefix(tmp_path, surface, declaration, defect, outcome):
    code, records = run_cache_case(tmp_path, surface, declaration, "test_compaction_reuse", defect)
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records
    if defect == "session_by_head":
        assert "compacted read 0" in records[0]["reason"], records


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (BLOCKS, None, "pass"),
        ({**RATIO, "cold_max_ratio": 0.1}, None, "pass"),
        (BLOCKS, "unchained_blocks", "fail"),
        ({**RATIO, "cold_max_ratio": 0.1}, "unchained_blocks", "fail"),
        # Nothing is cached: the warm control fails, so an empty cache cannot pass the case.
        (BLOCKS, "cold", "fail"),
    ],
)
def test_changed_head_reads_nothing(tmp_path, surface, declaration, defect, outcome):
    code, records = run_cache_case(
        tmp_path, surface, declaration, "test_changed_head_reads_nothing", defect
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


def test_changed_head_skips_without_a_cold_rule(tmp_path):
    code, records = run_cache_case(tmp_path, "chat_completions", RATIO, "test_changed_head")
    assert code == 0 and records[0]["outcome"] == "skip", records


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (BLOCKS, None, "pass"),
        (RATIO, None, "pass"),
        # The whole system prompt places the request, so a changed clock line moves it.
        (BLOCKS, "system_keyed", "fail"),
        (RATIO, "system_keyed", "fail"),
    ],
)
def test_changed_tail_costs_only_the_tail(tmp_path, surface, declaration, defect, outcome):
    code, records = run_cache_case(
        tmp_path, surface, declaration, "test_changed_tail_costs_only_the_tail", defect
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


@pytest.mark.parametrize("select", ["test_native_cache_reuse", "test_turn_reuse"])
def test_repeat_and_turn_cases_miss_a_system_keyed_layer(tmp_path, select):
    code, records = run_cache_case(tmp_path, "messages", BLOCKS, select, "system_keyed")
    assert code == 0 and {r["outcome"] for r in records} == {"pass"}, records


def test_scenario_checks_are_a_closed_vocabulary(tmp_path):
    from llmdv.profile.loader import ProfileError, load
    from tests.test_cache_reuse import cache_profile

    path = cache_profile(tmp_path, {"min_ratio": 0.8, "single_replica": True})
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"]["scenario_checks"] = ["inline_sytem"]
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ProfileError, match="scenario_checks"):
        load(path)


EQUIVALENT = {"render_equivalent": SURFACES}


def run_all_surfaces(tmp_path, declaration, select, defect=None, **capabilities):
    """One case on every cache surface of one fake ingress."""
    from tests.fakes import FakeFleet
    from tests.test_suites_against_fake import run_suites, write_profile

    with FakeFleet(cache_probe=True, cache_defect=defect, cache_block=16) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = SURFACES
        cache = {"surfaces": SURFACES, "prefix_words": 128, **declaration}
        raw["capabilities"] = {"cache_reuse": cache, **capabilities}
        path.write_text(yaml.safe_dump(raw))
        return run_suites(path, tmp_path / "run", "-k", select)


@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        ({**BLOCKS, **EQUIVALENT}, None, "pass"),
        ({**RATIO, **EQUIVALENT}, None, "pass"),
        # Messages renders its system blocks its own way, so no other API reads them.
        ({**BLOCKS, **EQUIVALENT}, "messages_render_differs", "fail"),
    ],
)
def test_cross_api_reads_the_other_api(tmp_path, declaration, defect, outcome):
    code, records = run_all_surfaces(tmp_path, declaration, "test_cross_api_reuse", defect)
    assert code == (1 if outcome == "fail" else 0), records
    outcomes = {r["variant"]: r["outcome"] for r in records}
    if outcome == "pass":
        assert set(outcomes.values()) == {"pass"}, records
    else:
        # Every surface either sent first to Messages or read after it.
        assert set(outcomes.values()) == {"fail"}, records


def test_cross_api_skips_undeclared_surfaces(tmp_path):
    declaration = {**BLOCKS, "render_equivalent": ["chat_completions", "responses"]}
    code, records = run_all_surfaces(tmp_path, declaration, "test_cross_api_reuse")
    outcomes = {r["variant"]: r["outcome"] for r in records}
    assert code == 0 and outcomes["messages"] == "skip", records
    assert outcomes["chat_completions"] == outcomes["responses"] == "pass", records


ATTRIBUTION = {"stripped_system_prefix": "x-llmdv-attribution:"}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        ({**BLOCKS, **ATTRIBUTION}, None, "pass"),
        ({**RATIO, **ATTRIBUTION}, None, "pass"),
        ({**BLOCKS, **ATTRIBUTION}, "attribution_kept", "fail"),
        ({**RATIO, **ATTRIBUTION}, "attribution_kept", "fail"),
        (BLOCKS, None, "skip"),
    ],
)
def test_attribution_line_is_stripped_where_declared(
    tmp_path, surface, declaration, defect, outcome
):
    code, records = run_cache_case(
        tmp_path, surface, declaration, "test_attribution_line_reuse", defect
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


HINTS = {"scenario_checks": ["cache_hints"]}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("defect,outcome", [(None, "pass"), ("hints_rendered", "fail")])
def test_cache_hints_leave_the_prompt_alone(tmp_path, surface, defect, outcome):
    code, records = run_cache_case(
        tmp_path, surface, BLOCKS, "test_cache_hints_reuse", defect, **HINTS
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


@pytest.mark.parametrize(
    "surface,fields",
    [
        (
            "chat_completions",
            ["prompt_cache_key", "prompt_cache_options", "prompt_cache_retention"],
        ),
        ("responses", ["prompt_cache_key", "prompt_cache_options", "prompt_cache_retention"]),
        ("messages", "system cache marker ttl"),
    ],
)
def test_cache_hints_send_each_surface_form(tmp_path, surface, fields):
    code, records = run_cache_case(tmp_path, surface, BLOCKS, "test_cache_hints_reuse", **HINTS)
    assert code == 0, records
    sent = next(c["observed"] for c in records[0]["checks"] if c["name"] == "cache_hint_fields")
    assert sent == fields, sent


@pytest.mark.parametrize(
    "override",
    [
        {"render_equivalent": ["chat_completions"]},
        {"render_equivalent": ["chat_completions", "chat_completions"]},
        {"render_equivalent": ["chat_completions", "messages"]},
        {"render_equivalent": "chat_completions"},
        {"stripped_system_prefix": ""},
        {"stripped_system_prefix": 3},
    ],
)
def test_render_declarations_are_strict(tmp_path, override):
    from llmdv.profile.loader import ProfileError, load
    from tests.test_cache_reuse import cache_profile

    path = cache_profile(tmp_path, {"min_ratio": 0.8, "single_replica": True, **override})
    with pytest.raises(ProfileError, match="cache_reuse"):
        load(path)


SALT = {"vllm_options": ["cache_salt"]}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (BLOCKS, None, "pass"),
        ({**RATIO, "cold_max_ratio": 0.1}, None, "pass"),
        (BLOCKS, "ignored_salt", "fail"),
        # The salt reaches the first turn only, so turn 2 reads nothing of turn 1.
        (BLOCKS, "salt_lost_in_session", "fail"),
    ],
)
def test_salted_session_stays_warm_and_isolated(tmp_path, surface, declaration, defect, outcome):
    code, records = run_cache_case(
        tmp_path, surface, declaration, "test_salted_session", defect, **SALT
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


@pytest.mark.parametrize("defect,outcome", [(None, "pass"), ("reasoning_blind", "fail")])
def test_each_responses_effort_reuses_its_own_prefix(tmp_path, defect, outcome):
    code, records = run_cache_case(
        tmp_path,
        "responses",
        BLOCKS,
        "test_responses_effort_cache_reuse",
        defect,
        responses_reasoning_efforts=["low", "high"],
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert ran(records) == {"low": outcome, "high": outcome}, records


@pytest.mark.parametrize("defect,outcome", [(None, "pass"), ("reasoning_blind", "fail")])
def test_each_messages_effort_reuses_its_own_prefix(tmp_path, defect, outcome):
    code, records = run_cache_case(
        tmp_path,
        "messages",
        BLOCKS,
        "test_anthropic_effort_cache_reuse",
        defect,
        messages_efforts=["low"],
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert ran(records) == {"low": outcome}, records


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "select,defect,outcome",
    [
        ("test_large_toolset_cache_reuse", None, "pass"),
        ("test_large_toolset_cache_reuse", "many_tools_unstable", "fail"),
        # Two tools never reach the threshold, so the existing case cannot see it.
        ("test_tools_cache_reuse", "many_tools_unstable", "pass"),
    ],
)
def test_many_tools_need_a_stable_render(tmp_path, surface, select, defect, outcome):
    code, records = run_cache_case(tmp_path, surface, BLOCKS, select, defect, model=TOOLS)
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


PARALLEL = {"scenario_checks": ["parallel_subagents"]}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (BLOCKS, None, "pass"),
        (RATIO, None, "pass"),
        # Concurrent requests for one prefix land on cold replicas; sequential ones do not.
        (BLOCKS, "concurrent_cold", "fail"),
    ],
)
def test_parallel_subagents_read_the_shared_prefix(tmp_path, surface, declaration, defect, outcome):
    code, records = run_cache_case(
        tmp_path,
        surface,
        declaration,
        "test_parallel_subagents_read_the_shared_prefix",
        defect,
        **PARALLEL,
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records


def test_parallel_subagents_are_excluded_on_shared_fleets(tmp_path):
    from tests.fakes import FakeFleet
    from tests.test_suites_against_fake import run_suites, write_profile

    with FakeFleet(cache_probe=True) as fleet:
        path = write_profile(tmp_path, fleet, fleet_policy="shared")
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"] = {"cache_reuse": {**BLOCKS, "prefix_words": 128}, **PARALLEL}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path, tmp_path / "run", "-k", "test_parallel_subagents_read_the_shared_prefix"
        )
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "[excluded_by_policy]" in records[0]["reason"], records
