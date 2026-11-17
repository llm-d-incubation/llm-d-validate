"""The statistical check: declared n, a closed rule set, every sample on record.

The rule tests are property-style: each draws many sample lists from a seeded generator and
holds the verdict to the rule's arithmetic, computed here independently of the module.
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter
from statistics import median

import pytest

from llmdv import plugin
from llmdv.assertions.samples import Rule, draw, judge, resolve, sample
from llmdv.evidence import journal
from llmdv.profile.errors import ProfileError
from llmdv.profile.schema import SampleRule

TRIALS = 300


@pytest.fixture
def active():
    yield journal.activate("samples")
    journal.deactivate()


def _verdict(name, rule, samples, **kwargs):
    """(passed, observed) for one judgment, without letting a failure end the test."""
    try:
        return True, judge(name, rule, samples, **kwargs)
    except AssertionError:
        return False, journal.current().checks[-1].observed


def _texts(rng, n):
    return [sample(rng.choice(["a", "b", "c"][: rng.randint(1, 3)]), i) for i in range(n)]


def test_count_holds_exactly_when_its_arithmetic_does(active):
    rng = random.Random(7)
    for _ in range(TRIALS):
        n = rng.randint(1, 8)
        low = rng.randint(0, n)
        rule = Rule("count", n, min=low, max=rng.randint(low, n))
        drawn = _texts(rng, n)
        mode = max(Counter(s.value for s in drawn).values())
        passed, observed = _verdict("c", rule, drawn)
        assert passed is (rule.min <= mode <= rule.max), (rule, drawn)
        assert observed["count"] == mode and observed["holds"] is passed


def test_count_against_a_named_target(active):
    rng = random.Random(8)
    for _ in range(TRIALS):
        n = rng.randint(1, 8)
        flags = [rng.random() < 0.5 for _ in range(n)]
        k = rng.randint(0, n)
        rule = Rule("count", n, min=k, target=True)
        passed, _ = _verdict("parity", rule, [sample(f, i) for i, f in enumerate(flags)])
        assert passed is (sum(flags) >= k)


def test_all_equal_and_all_distinct_are_count_bounds(active):
    same = [sample("x", i) for i in range(4)]
    apart = [sample(t, i) for i, t in enumerate("wxyz")]
    assert _verdict("eq", Rule("count", 4, min=4), same)[0]
    assert not _verdict("eq", Rule("count", 4, min=4), apart)[0]
    assert _verdict("distinct", Rule("count", 4, max=1), apart)[0]
    assert not _verdict("distinct", Rule("count", 4, max=1), same)[0]


def test_ratio_holds_exactly_when_the_median_ratio_is_in_bounds(active):
    rng = random.Random(9)
    for _ in range(TRIALS):
        n = rng.randint(1, 6)
        rule = Rule("ratio", n, min=0.8, max=1.25).over(["a", "b"])
        values = {g: [rng.randint(1, 20) for _ in range(n)] for g in "ab"}
        drawn = [sample(v, None, g) for g in "ab" for v in values[g]]
        ratio = median(values["a"]) / median(values["b"])
        assert _verdict("r", rule, drawn)[0] is (0.8 <= ratio <= 1.25)


def test_ratio_over_a_zero_median_fails(active):
    rule = Rule("ratio", 1, min=0).over(["a", "b"])
    passed, observed = _verdict("r", rule, [sample(1, 0, "a"), sample(0, 1, "b")])
    assert not passed and observed["ratio"] is None


def test_order_holds_exactly_when_the_medians_follow_the_declared_order(active):
    rng = random.Random(10)
    groups = ["low", "medium", "high"]
    for _ in range(TRIALS):
        n = rng.randint(1, 5)
        slack = rng.choice([1, 1.25, 2])
        rule = Rule("order", n, slack=slack).over(groups)
        values = {g: [rng.randint(1, 30) for _ in range(n)] for g in groups}
        medians = {g: median(v) for g, v in values.items()}
        drawn = [sample(v, None, g) for g in groups for v in values[g]]
        expected = medians["low"] < medians["high"] and not any(
            medians[a] > medians[b] * slack for i, a in enumerate(groups) for b in groups[i + 1 :]
        )
        assert _verdict("o", rule, drawn)[0] is expected, (values, slack)


def test_draw_takes_exactly_n_per_group_without_stopping_early():
    calls = []

    def take(group):
        calls.append(group)
        return sample(1, len(calls) - 1, group)

    rule = Rule("order", 4, slack=1).over(["a", "b", "c"])
    drawn = draw(rule, take)
    assert calls == ["a", "b", "c"] * 4  # round-robin, all of them, even once decided
    assert len(drawn) == 12
    assert len(draw(Rule("count", 5, min=5), lambda g: sample("same", None, g))) == 5


def test_judge_refuses_any_other_number_of_samples(active):
    with pytest.raises(ValueError, match="expected 3 samples"):
        judge("c", Rule("count", 3, min=3), [sample("a", 0), sample("a", 1)])


def test_every_sample_is_recorded_with_its_rule(active):
    rule = Rule("count", 3, min=3)
    texts = ["Purple owls whisper", "Purple owls whisper", "Seven quiet " + "x" * 100]
    with pytest.raises(AssertionError, match="repeat_text"):
        judge("repeat_text", rule, [sample(t, i) for i, t in enumerate(texts)])
    (check,) = active.checks
    assert check.ok is False
    assert check.observed == {"count": 2, "n": 3, "distinct": 2, "holds": False}
    assert check.rule == {"kind": "count", "n": 3, "target": "mode", "min": 3, "max": 3}
    assert [s["exchange"] for s in check.samples] == [0, 1, 2]
    assert all(s["value"].startswith("sha256:") and len(s["value"]) == 19 for s in check.samples)
    assert check.samples[0]["value"] == check.samples[1]["value"] != check.samples[2]["value"]
    assert check.samples[0]["preview"] == "Purple owls whisper"
    assert len(check.samples[2]["preview"]) == 60
    assert set(check.to_dict()) == {"name", "observed", "expected", "ok", "rule", "samples"}


def test_a_measurement_passes_and_says_whether_the_rule_held(active):
    observed = judge("m", Rule("count", 2, min=2), [sample("a", 0), sample("b", 1)], decides=False)
    (check,) = active.checks
    assert check.ok is True and observed["holds"] is False
    assert check.expected.startswith("measured, not judged")


def test_an_ordinary_check_records_no_rule_or_samples(active):
    journal.record("plain", 1)
    assert set(active.checks[0].to_dict()) == {"name", "observed", "expected", "ok"}


@pytest.mark.parametrize(
    "rule",
    [
        dict(kind="vote", n=2),
        dict(kind="count", n=0),
        dict(kind="count", n=2, min=3),
        dict(kind="count", n=3, min=1.5),
        dict(kind="count", n=2, slack=1.5),
        dict(kind="ratio", n=2),
        dict(kind="order", n=2, slack=0.5),
        dict(kind="order", n=2, min=1),
    ],
)
def test_an_inconsistent_rule_is_refused(rule):
    with pytest.raises(ValueError):
        Rule(**rule)


def test_a_profile_entry_overrides_n_and_bounds():
    where = "capabilities.samples.openai_chat.greedy_repeat"
    default = Rule("count", 2, min=2)
    assert resolve(default, None, where) is default
    resolved = resolve(default, SampleRule(n=5, min=4), where)
    assert (resolved.n, resolved.bounds()) == (5, (4, 5))
    assert resolve(default, SampleRule(n=3, min=2), where).bounds() == (2, 3)
    order = resolve(Rule("order", 3, slack=1.25), SampleRule(n=5), where)
    assert (order.n, order.slack) == (5, 1.25)


def test_a_bound_the_entry_leaves_unset_keeps_the_cases_default():
    where = "capabilities.samples.openai_chat.seed_repeat"
    repeat = resolve(Rule("count", 2, min=2), SampleRule(max=2), where)
    assert repeat.bounds() == (2, 2)
    fresh = resolve(Rule("count", 2, max=1), SampleRule(n=3, min=0), where)
    assert (fresh.n, fresh.bounds()) == (3, (0, 1))
    ratio = resolve(Rule("ratio", 3, min=0.5, max=2), SampleRule(max=4), where)
    assert ratio.bounds() == (0.5, 4)


def test_an_entry_that_contradicts_a_kept_default_is_refused_naming_both():
    where = "ingresses[edge].capabilities.samples.openai_chat.seed_repeat"
    with pytest.raises(ProfileError) as caught:
        resolve(Rule("count", 2, min=2), SampleRule(max=1), where)
    message = str(caught.value)
    assert message.startswith(f"{where}: expected 0 <= min <= max <= n (2), got 2, 1")
    assert "keeps the case's default (n 2, min 2)" in message


@pytest.mark.parametrize(
    "default,entry",
    [
        (Rule("count", 2, max=1), SampleRule(n=1, max=1)),
        (Rule("count", 2, min=2), SampleRule(n=1, min=1)),
        (Rule("count", 2, min=2), SampleRule(min=1)),
        (Rule("count", 2, min=2), SampleRule(n=4, min=0)),
        (Rule("count", 2, min=1, target=True), SampleRule(min=0)),
    ],
)
def test_a_count_rule_no_samples_can_fail_is_refused(default, entry):
    where = "ingresses[edge].capabilities.samples.openai_chat.fresh_generation"
    with pytest.raises(ProfileError, match="can never fail") as caught:
        resolve(default, entry, where)
    assert str(caught.value).startswith(where)


def test_a_rule_some_samples_can_fail_is_kept(active):
    """Whatever resolve accepts, some sample set fails: all equal, or all different."""
    where = "capabilities.samples.case"
    rng = random.Random(11)
    for _ in range(TRIALS):
        n = rng.randint(1, 6)
        low = rng.randint(0, n)
        entry = SampleRule(n=n, min=low, max=rng.randint(low, n))
        try:
            rule = resolve(Rule("count", 2, min=2), entry, where)
        except ProfileError:
            low, high = entry.min, entry.max
            assert low <= 1 and high >= n, entry
            continue
        same = [sample("x", i) for i in range(n)]
        apart = [sample(str(i), i) for i in range(n)]
        assert not (_verdict("c", rule, same)[0] and _verdict("c", rule, apart)[0]), entry


@pytest.mark.parametrize(
    "rule",
    [
        dict(kind="order", n=3, slack=math.nan),
        dict(kind="order", n=3, slack=math.inf),
        dict(kind="ratio", n=3, min=math.nan),
        dict(kind="ratio", n=3, min=1, max=math.inf),
        dict(kind="ratio", n=3, max=math.nan),
        dict(kind="count", n=3, min=math.nan),
        dict(kind="count", n=3, max=math.inf),
        dict(kind="order", n=3, slack=True),
    ],
)
def test_a_bound_that_is_not_a_finite_number_is_refused(rule):
    with pytest.raises(ValueError, match="expected a(n integer| finite number| number)"):
        Rule(**rule)
    entry = SampleRule(**{k: v for k, v in rule.items() if k not in ("kind", "n")})
    default = {"order": Rule("order", 3, slack=1.25), "ratio": Rule("ratio", 3, min=0.5)}.get(
        rule["kind"], Rule("count", 3, min=3)
    )
    with pytest.raises(ProfileError, match="capabilities.samples.case"):
        resolve(default, entry, "capabilities.samples.case")


def test_the_expected_text_describes_the_rule_applied(active):
    """The case's wording stands for its own default; a profile's rule is described as set."""
    where = "capabilities.samples.openai_chat.fresh_generation"
    default = Rule("count", 2, max=1)
    judge("fresh", resolve(default, None, where), [sample("a", 0), sample("b", 1)], "all differ")
    wider = resolve(default, SampleRule(n=3, max=2), where)
    judge("fresh", wider, [sample(t, i) for i, t in enumerate("aab")], "all differ")
    judge("m", wider, [sample(t, i) for i, t in enumerate("aab")], "all differ", decides=False)
    same = resolve(default, SampleRule(max=1), where)
    judge("fresh", same, [sample("a", 0), sample("b", 1)], "all differ")
    assert [c.expected for c in active.checks] == [
        "all differ",
        "0 to 2 of 3 samples equal the most common (set by the profile)",
        "measured, not judged: 0 to 2 of 3 samples equal the most common (set by the profile)",
        "all differ",
    ]


def test_a_whole_float_and_its_integer_are_one_value(active):
    drawn = [sample(1, 0), sample(1.0, 1), sample(2, 2)]
    assert _verdict("t", Rule("count", 3, min=2, target=1), drawn)[1]["count"] == 2
    assert _verdict("t", Rule("count", 3, min=2, target=1.0), drawn)[1]["count"] == 2
    assert _verdict("m", Rule("count", 3, min=2), drawn)[1] == {
        "count": 2,
        "n": 3,
        "distinct": 2,
        "holds": True,
    }
    flags = [sample(True, 0), sample(1, 1), sample(1, 2)]
    assert _verdict("b", Rule("count", 3, min=1, target=True), flags)[1]["count"] == 1


def test_a_target_spelled_mode_is_a_value_not_the_most_common(active):
    drawn = [sample("x", 0), sample("x", 1), sample("mode", 2)]
    named = Rule("count", 3, min=2, target="mode")
    passed, observed = _verdict("named", named, drawn)
    assert not passed and observed["count"] == 1
    assert named.to_dict()["target"] == sample("mode").value
    assert Rule("count", 3, min=2).to_dict()["target"] == "mode"
    assert _verdict("common", Rule("count", 3, min=2), drawn)[0]


@pytest.mark.parametrize("odd", [None, "many", True, math.nan, math.inf])
@pytest.mark.parametrize(
    "rule",
    [Rule("order", 2, slack=1.25).over(["a", "b"]), Rule("ratio", 2, min=0.5).over(["a", "b"])],
)
def test_a_sample_that_is_not_a_number_fails_an_order_or_ratio_check(active, rule, odd):
    """A fleet that omits a count fails the check with every sample on record, not a TypeError."""
    drawn = [sample(1, 0, "a"), sample(odd, 1, "a"), sample(5, 2, "b"), sample(6, 3, "b")]
    passed, observed = _verdict("o", rule, drawn)
    assert not passed and observed == {"not_numbers": 1, "holds": False}
    (check,) = active.checks
    assert check.ok is False and len(check.samples) == 4
    strict = json.dumps(check.to_dict(), allow_nan=False)
    assert json.loads(strict)["samples"][1]["exchange"] == 1


def test_a_non_finite_sample_is_recorded_as_strict_json(active):
    drawn = [sample(math.nan, 0), sample(math.nan, 1), sample(-math.inf, 2)]
    assert [s.value for s in drawn] == ["nan", "nan", "-inf"]
    passed, observed = _verdict("c", Rule("count", 3, min=3), drawn)
    assert not passed and observed["count"] == 2
    json.dumps(active.checks[0].to_dict(), allow_nan=False)


@pytest.mark.parametrize(
    "entry,message",
    [
        (SampleRule(n=5), "also sets min or max"),
        (SampleRule(slack=2), "the count rule takes min and max"),
        (SampleRule(min=3), "min <= max <= n"),
    ],
)
def test_a_profile_entry_the_rule_cannot_take_is_refused(entry, message):
    where = "capabilities.samples.openai_chat.greedy_repeat"
    with pytest.raises(ProfileError, match=message) as caught:
        resolve(Rule("count", 2, min=2), entry, where)
    assert str(caught.value).startswith(where)


class _Case:
    """A collected item with a case id and a `samples` marker, as the plugin reads them."""

    def __init__(self, case_id, *args, **kwargs):
        self.markers = {
            "case_id": pytest.mark.case_id(case_id).mark,
            "samples": pytest.mark.samples(*args, **kwargs).mark,
        }

    def get_closest_marker(self, name):
        return self.markers.get(name)


def test_a_case_whose_default_rule_can_never_fail_is_refused_at_collection():
    profile = type("Profile", (), {"ingresses": ()})()
    plugin._check_sample_rules(profile, [_Case("x.sound", "count", n=2, min=2)])
    with pytest.raises(pytest.UsageError, match="x.idle: its samples marker .* can never fail"):
        plugin._check_sample_rules(profile, [_Case("x.idle", "count", n=1, min=1)])
