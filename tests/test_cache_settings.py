"""The setting-equivalence case, proven to fail on a render that keeps settings apart."""

import pytest

from llmdv.profile.schema import EffortRendering
from llmdv.suites.cache.test_settings import template_effort
from tests.test_cache_reuse import BLOCKS, RATIO
from tests.test_cache_sessions import EQUIVALENT, run_all_surfaces

SETTINGS = {"equivalent_settings": [["messages:disabled", "chat_completions:low"]]}


@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        ({**BLOCKS, **SETTINGS}, None, "pass"),
        ({**RATIO, **SETTINGS}, None, "pass"),
        ({**BLOCKS, **SETTINGS}, "settings_apart", "fail"),
    ],
)
def test_equivalent_settings_read_each_other(tmp_path, declaration, defect, outcome):
    code, records = run_all_surfaces(tmp_path, declaration, "test_setting_equivalence", defect)
    assert code == (1 if outcome == "fail" else 0), records
    outcomes = {r["variant"]: r["outcome"] for r in records}
    # Each side of the pair runs once, after the other; Responses is in no pair.
    assert outcomes == {"chat_completions": outcome, "messages": outcome, "responses": "skip"}
    sent = [
        c["observed"]
        for r in records
        for c in r["checks"]
        if c["name"] == "equivalent_settings" and r["variant"] == "messages"
    ]
    assert sent == [[["chat_completions:low", "messages:disabled"]]], sent


MEDIUM_DEFAULT = {"equivalent_settings": [["chat_completions:medium", "messages:enabled"]]}
MEDIUM_BUDGETED = {"equivalent_settings": [["chat_completions:medium", "messages:enabled:2048"]]}
LOW_DEFAULT = {"equivalent_settings": [["chat_completions:low", "messages:enabled"]]}


@pytest.mark.parametrize(
    "settings,defect,outcome",
    [
        # A gateway that maps a Messages budget onto an effort in tiers renders the default
        # budget of 1024 as low, so it reads nothing a medium Chat request computed.
        (MEDIUM_DEFAULT, "budget_tiers", "fail"),
        (LOW_DEFAULT, "budget_tiers", "pass"),
        (MEDIUM_BUDGETED, "budget_tiers", "pass"),
        (MEDIUM_BUDGETED, None, "pass"),
        # The budget does not excuse a render that keeps the settings or Messages apart.
        (MEDIUM_BUDGETED, "settings_apart", "fail"),
        (MEDIUM_BUDGETED, "messages_render_differs", "fail"),
    ],
)
def test_enabled_thinking_budget_reaches_the_gateway(tmp_path, settings, defect, outcome):
    code, records = run_all_surfaces(
        tmp_path, {**BLOCKS, **settings}, "test_setting_equivalence", defect
    )
    assert code == (1 if outcome == "fail" else 0), records
    outcomes = {r["variant"]: r["outcome"] for r in records}
    assert outcomes == {"chat_completions": outcome, "messages": outcome, "responses": "skip"}
    entry = settings["equivalent_settings"][0][1]
    sent = [
        c["observed"]
        for r in records
        for c in r["checks"]
        if c["name"] == "equivalent_settings" and r["variant"] == "chat_completions"
    ]
    assert sent == [[[entry, settings["equivalent_settings"][0][0]]]], sent


def test_cross_api_misses_settings_kept_apart(tmp_path):
    """The cross-API case sends no reasoning setting, so the defect never shows there."""
    code, records = run_all_surfaces(
        tmp_path, {**BLOCKS, **EQUIVALENT}, "test_cross_api_reuse", "settings_apart"
    )
    assert code == 0, records
    assert {r["outcome"] for r in records} == {"pass"}, records


def test_enabled_thinking_carries_a_budget_below_max_tokens():
    from llmdv.suites.cache.test_settings import setting_fields

    name, fields = setting_fields("messages:enabled")
    assert name == "messages"
    assert fields["thinking"]["budget_tokens"] < fields["max_tokens"]
    assert setting_fields("responses:low") == ("responses", {"reasoning": {"effort": "low"}})


@pytest.mark.parametrize(
    "entry,budget",
    [("messages:enabled", 1024), ("messages:enabled:1024", 1024), ("messages:enabled:4096", 4096)],
)
def test_enabled_thinking_sends_the_entry_budget(entry, budget):
    from llmdv.suites.cache.test_settings import setting_fields

    name, fields = setting_fields(entry)
    assert name == "messages"
    assert fields["thinking"] == {"type": "enabled", "budget_tokens": budget}
    assert fields["max_tokens"] > budget


def test_budgeted_entries_load(tmp_path):
    from llmdv.profile.loader import load
    from tests.test_cache_reuse import cache_profile

    pairs = [
        ["chat_completions:low", "messages:enabled"],
        ["chat_completions:medium", "messages:enabled:2048"],
        ["messages:enabled", "messages:enabled:2048"],
    ]
    declaration = {"min_ratio": 0.8, "single_replica": True, "equivalent_settings": pairs}
    declaration["surfaces"] = ["chat_completions", "messages"]
    profile = load(cache_profile(tmp_path, declaration))
    loaded = {
        pair
        for ingress in profile.ingresses
        for pair in ingress.capabilities.cache_reuse.equivalent_settings
    }
    assert loaded == {tuple(pair) for pair in pairs}


@pytest.mark.parametrize(
    "settings",
    [
        [["messages:disabled"]],
        [["messages:disabled", "messages:disabled"]],
        [["messages:low", "chat_completions:low"]],
        [["messages:disabled", "completions:low"]],
        [["messages:disabled", "responses:low"]],
        # A budget only on Messages `enabled`, a whole number of at least 1024.
        [["messages:enabled:1023", "chat_completions:low"]],
        [["messages:enabled:02048", "chat_completions:low"]],
        [["messages:enabled:2048.0", "chat_completions:low"]],
        [["messages:enabled:", "chat_completions:low"]],
        [["messages:enabled:-2048", "chat_completions:low"]],
        [["messages:enabled:2048:1", "chat_completions:low"]],
        [["messages:adaptive:2048", "chat_completions:low"]],
        [["messages:disabled:2048", "chat_completions:low"]],
        [["chat_completions:low:2048", "messages:enabled"]],
        # The default budget spelled out is the same entry.
        [["messages:enabled", "messages:enabled:1024"]],
        ["messages:disabled"],
        "messages:disabled",
    ],
)
def test_equivalent_settings_are_strict(tmp_path, settings):
    from llmdv.profile.loader import ProfileError, load
    from tests.test_cache_reuse import cache_profile

    declaration = {"min_ratio": 0.8, "single_replica": True, "equivalent_settings": settings}
    declaration["surfaces"] = ["chat_completions", "messages"]
    path = cache_profile(tmp_path, declaration)
    with pytest.raises(ProfileError, match="equivalent_settings"):
        load(path)


KWARGS = "test_kwargs_effort_reuse"


def run_kwargs(tmp_path, declaration, defect=None, options=("chat_template_kwargs",), **caps):
    from tests.test_cache_reuse import run_cache_case

    capabilities = {"vllm_options": list(options), "chat_quiet_efforts": ["low"], **caps}
    return run_cache_case(tmp_path, "chat_completions", declaration, KWARGS, defect, **capabilities)


@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (BLOCKS, None, "pass"),
        (RATIO, None, "pass"),
        (BLOCKS, "kwargs_unrendered", "fail"),
        (RATIO, "kwargs_unrendered", "fail"),
    ],
)
def test_kwargs_effort_reads_the_top_level_effort(tmp_path, declaration, defect, outcome):
    code, records = run_kwargs(tmp_path, declaration, defect)
    assert code == (1 if outcome == "fail" else 0), records
    assert [r["outcome"] for r in records] == [outcome], records
    forms = next(c["observed"] for c in records[0]["checks"] if c["name"] == "effort_forms")
    assert forms == [
        {"reasoning_effort": "low"},
        {"chat_template_kwargs": {"reasoning_effort": "low"}},
    ]


def test_kwargs_effort_skips_without_the_option(tmp_path):
    code, records = run_kwargs(tmp_path, BLOCKS, options=())
    assert code == 0 and records[0]["outcome"] == "skip", records
    assert "[undeclared]" in records[0]["reason"], records


def test_effort_repeat_misses_a_render_that_drops_kwargs(tmp_path):
    """The effort repeat sends the top-level field twice, so a render blind to kwargs passes it."""
    from tests.test_cache_reuse import run_cache_case

    code, records = run_cache_case(
        tmp_path,
        "chat_completions",
        BLOCKS,
        "test_reasoning_effort_cache_reuse",
        "kwargs_unrendered",
        chat_reasoning_efforts=["low"],
    )
    assert code == 0, records
    assert [r["outcome"] for r in records if r["outcome"] != "skip"] == ["pass"], records


# A measured rendering: the template knows low, medium, high and xhigh; the deployment's
# map sends none and minimal to low and max to xhigh, on the top-level field only.
MAPPED_MARKERS = {
    "unset": "512",
    "none": "8",
    "minimal": "8",
    "low": "8",
    "medium": "32",
    "high": "256",
    "xhigh": "512",
    "max": "512",
}
MAPPED = {
    "chat_quiet_efforts": ["none"],
    "chat_reasoning_efforts": ["minimal", "low", "medium", "high", "xhigh", "max"],
    "effort_rendering": {
        "pattern": r"Reasoning strength: (\d+)\.",
        "surfaces": ["chat_completions"],
        "efforts": MAPPED_MARKERS,
    },
}


@pytest.mark.parametrize(
    "defect,outcome",
    [
        # `none` as a kwarg skips the map and answers 400 by design: the case sends medium.
        ("kwargs_skip_effort_map", "pass"),
        ("kwargs_unrendered", "fail"),
    ],
)
def test_kwargs_effort_is_one_the_template_takes_as_itself(tmp_path, defect, outcome):
    code, records = run_kwargs(tmp_path, BLOCKS, defect, **MAPPED)
    assert code == (1 if outcome == "fail" else 0), records
    assert [r["outcome"] for r in records] == [outcome], records
    forms = next(c["observed"] for c in records[0]["checks"] if c["name"] == "effort_forms")
    assert forms == [
        {"reasoning_effort": "medium"},
        {"chat_template_kwargs": {"reasoning_effort": "medium"}},
    ]


def test_a_mapped_kwargs_effort_is_refused_by_the_template(tmp_path):
    """What the rule avoids: with no rendering declared the first effort, `none`, is sent."""
    code, records = run_kwargs(
        tmp_path, BLOCKS, "kwargs_skip_effort_map", **{**MAPPED, "effort_rendering": None}
    )
    assert code == 1 and records[0]["outcome"] == "fail", records
    assert "400" in records[0]["reason"], records


def _rendering(markers, surfaces=("chat_completions",)):
    return EffortRendering(pattern=r"(\w+)", efforts=markers, surfaces=surfaces)


@pytest.mark.parametrize(
    "efforts,rendering,expected",
    [
        # No Chat rendering declared: the first effort, as before.
        (("none", "low"), None, "none"),
        (("none", "low"), _rendering({"none": "8", "low": "8"}, ("responses",)), "none"),
        # That rendering, whichever effort is listed first: medium is the first unique marker.
        (("none", "minimal", "low", "medium"), _rendering(MAPPED_MARKERS), "medium"),
        (("low", "minimal", "medium", "high"), _rendering(MAPPED_MARKERS), "medium"),
        # A marker that spells the quiet effort itself, though the map folds others onto it.
        (
            ("low", "high", "max"),
            _rendering({"unset": "Max", "none": "Low", "minimal": "Low", "low": "Low"}),
            "low",
        ),
        # A template where `none` renders no marker, so nothing shows a fold.
        (("none", "minimal"), _rendering({"none": None, "minimal": "50", "low": "50"}), "none"),
        (("none", "low"), _rendering({"unset": None, "none": None, "minimal": None}), "none"),
        # Every declared effort shares its marker with another: none qualifies.
        (("none", "minimal"), _rendering({"none": "8", "minimal": "8"}), None),
    ],
)
def test_template_effort_rule(efforts, rendering, expected):
    assert template_effort(efforts, rendering) == expected
