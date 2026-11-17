"""One check over n samples, for claims that hold statistically rather than per request.

Some claims are about a distribution: greedy decoding repeats, two sampled requests differ, a
lower reasoning effort reasons less than a higher one. One request per claim flaps, and a retry
until it passes hides the very behavior under test. So a case declares its samples up front:

    @pytest.mark.samples("count", n=2, min=2)

and the `samples` fixture resolves that default against the profile's `capabilities.samples`
entry for the case. The case then sends exactly `n` requests per group through `draw`, which
never stops early and never re-sends, and passes them all to `judge`, which records one check
holding the rule, the summary it judged and every sample, each linked to its request's `seq` in
exchanges.jsonl.

The rule kinds are closed:

- `count`: the number of samples equal to the target (the most common value, or a named one)
  lies within [min, max]. All equal is `min: n`, k of n is `min: k`, all distinct is `max: 1`.
  Bounds count samples, so they are integers; `min` defaults to 0 and `max` to n. A whole
  float and its integer are one value.
- `ratio`: median(a) / median(b) lies within [min, max]. A zero median below the line fails.
- `order`: the groups' medians follow the declared order: the first is strictly below the last,
  and no lower group's median exceeds a higher one's by more than the factor `slack`.

A verdict is pass or fail. A failed sample request fails its own check before the rule is
reached, so the rule always sees n samples per group. A `ratio` or `order` sample that is not a
finite number fails the check, with every sample on record.

Every bound is a finite number, and a sample that is not finite is recorded by name, so a rule
and its samples are always strict JSON. A profile's entry keeps the case's default for each
key it leaves unset, and an entry whose rule no sample set can fail is refused (`resolve`).
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from ..evidence import journal
from ..http.redact import redact_value, scrub
from ..profile.errors import ProfileError

KINDS = ("count", "ratio", "order")


class Target(enum.Enum):
    """What a count rule counts when it names no value. Not a string, so a sample whose text
    is `mode` stays a value a rule can name."""

    MODE = "mode"


MODE = Target.MODE
PREVIEW_CHARS = 60
# The bounds each kind takes; the profile may set these and n, never the kind or the target.
BOUNDS = {"count": ("min", "max"), "ratio": ("min", "max"), "order": ("slack",)}


@dataclass(frozen=True)
class Rule:
    """A declared statistical rule: its kind, its sample count and its bounds."""

    kind: str
    n: int
    min: float | None = None
    max: float | None = None
    # count only: `mode` (the most common sample) or a value the samples are compared with.
    target: Any = MODE
    # order only: how far a lower group's median may exceed a higher one's, as a factor.
    slack: float | None = None
    # ratio: (a, b); order: the declared order, lowest first. Empty for one group.
    groups: tuple[str, ...] = ()
    # The profile changed the case's default, so the check's claim describes this rule.
    from_profile: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown rule kind {self.kind!r}; known: {', '.join(KINDS)}")
        if isinstance(self.n, bool) or not isinstance(self.n, int) or self.n < 1:
            raise ValueError(f"n: expected a positive integer, got {self.n!r}")
        for name in ("min", "max", "slack"):
            value = getattr(self, name)
            if value is not None and name not in BOUNDS[self.kind]:
                takes = " and ".join(BOUNDS[self.kind])
                raise ValueError(f"{name}: the {self.kind} rule takes {takes}")
            if value is not None and not _finite(value):
                raise ValueError(f"{name}: expected a finite number, got {value!r}")
        low, high = self.bounds()
        if self.kind == "count":
            if any(isinstance(b, float) and not b.is_integer() for b in (low, high)):
                raise ValueError("min and max count samples, so they are integers")
            if not 0 <= low <= high <= self.n:
                raise ValueError(f"expected 0 <= min <= max <= n ({self.n}), got {low}, {high}")
        if self.kind == "ratio":
            if self.min is None and self.max is None:
                raise ValueError("the ratio rule takes min, max or both")
            if high is not None and low > high:
                raise ValueError(f"expected min <= max, got {low}, {high}")
        if self.kind == "order" and self.slack is not None and self.slack < 1:
            raise ValueError(f"slack: expected a factor of at least 1, got {self.slack!r}")

    def bounds(self) -> tuple[float, float | None]:
        """[min, max] with the defaults applied: 0 and n for count, 0 and none for ratio."""
        low = 0 if self.min is None else self.min
        high = self.max
        if self.kind == "count" and high is None:
            high = self.n
        return low, high

    def over(self, groups: Sequence[str]) -> Rule:
        """This rule over named groups: (a, b) for ratio, the declared order for order."""
        return dataclasses.replace(self, groups=tuple(groups))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "n": self.n}
        if self.kind == "count":
            low, high = self.bounds()
            target = MODE.value if self.target is MODE else _reduce(self.target)
            out |= {"target": target, "min": int(low), "max": int(high)}
        elif self.kind == "ratio":
            out |= {"min": self.bounds()[0], "max": self.max}
        else:
            out["slack"] = 1 if self.slack is None else self.slack
        if self.groups:
            out["groups"] = list(self.groups)
        return out


@dataclass(frozen=True)
class Sample:
    """One sample as recorded: the value judged, a short preview and its request."""

    value: Any
    preview: str
    exchange: int | None = None
    group: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"exchange": self.exchange, "value": self.value}
        if self.group is not None:
            out["group"] = self.group
        out["preview"] = self.preview
        return out


def sample(
    value: Any, exchange: int | None = None, group: str | None = None, preview: Any = None
) -> Sample:
    """A sample of `value`: a finite number, a boolean or null as itself, a number that is not
    finite by its name (`nan`, `inf`, `-inf`), anything else as a digest.

    `exchange` is the request's `seq` in exchanges.jsonl. The preview shows the start of
    `preview`, or of the value when none is given, redacted and at most PREVIEW_CHARS long.
    """
    shown = value if preview is None else preview
    text = shown if isinstance(shown, str) else json.dumps(shown, ensure_ascii=False)
    return Sample(_reduce(value), scrub(text)[:PREVIEW_CHARS], exchange, group)


def draw(rule: Rule, take: Callable[[str | None], Sample]) -> list[Sample]:
    """Exactly n samples per group, taken round-robin so drift in the fleet spreads evenly.

    `take(group)` sends one request and returns its sample; it is called n times per group and
    never more or less. A request that fails raises from its own check and ends the case.
    """
    groups: Sequence[str | None] = rule.groups or (None,)
    return [take(group) for _ in range(rule.n) for group in groups]


def judge(
    name: str, rule: Rule, samples: Sequence[Sample], expected: str = "", *, decides: bool = True
) -> dict[str, Any]:
    """Record one check judging `samples` by `rule`, and fail it when the rule does not hold.

    With `decides=False` the check is a measurement: it records the samples and whether the
    rule held, and passes either way. The returned summary is the check's observed value; its
    `holds` says whether the rule's arithmetic held.
    """
    groups = rule.groups or (None,)
    counts = Counter(s.group for s in samples)
    if set(counts) != set(groups) or any(counts[g] != rule.n for g in groups):
        raise ValueError(
            f"{name}: expected {rule.n} samples per group {list(groups)}, got {counts}"
        )
    odd = sum(not _finite(s.value) for s in samples) if rule.kind != "count" else 0
    # Medians of anything but numbers mean nothing: the check fails on what it was sent.
    observed = {"not_numbers": odd, "holds": False} if odd else _JUDGES[rule.kind](rule, samples)
    claim = expected or _describe(rule)
    if rule.from_profile:
        # The case worded its claim for its default rule; this one is the profile's.
        claim = f"{_describe(rule)} (set by the profile)"
    if not decides:
        claim = f"measured, not judged: {claim}"
    ok = observed["holds"] or not decides
    recorded = [redact_value(s.to_dict()) for s in samples]
    journal.record(name, observed, scrub(claim), ok, rule.to_dict(), recorded)
    if not ok:
        raise AssertionError(f"{name}: expected {scrub(claim)}, got {observed!r}")
    return observed


def _count(rule: Rule, samples: Sequence[Sample]) -> dict[str, Any]:
    values = Counter(_key(s.value) for s in samples)
    if rule.target is MODE:
        count = max(values.values())
    else:
        count = values[_key(_reduce(rule.target))]
    low, high = rule.bounds()
    return {"count": count, "n": rule.n, "distinct": len(values), "holds": low <= count <= high}


def _ratio(rule: Rule, samples: Sequence[Sample]) -> dict[str, Any]:
    a, b = (median(s.value for s in samples if s.group == g) for g in rule.groups)
    ratio = a / b if b else None
    low, high = rule.bounds()
    holds = ratio is not None and low <= ratio and (high is None or ratio <= high)
    return {"medians": dict(zip(rule.groups, (a, b), strict=True)), "ratio": ratio, "holds": holds}


def _order(rule: Rule, samples: Sequence[Sample]) -> dict[str, Any]:
    medians = {g: median(s.value for s in samples if s.group == g) for g in rule.groups}
    slack = 1 if rule.slack is None else rule.slack
    first, last = medians[rule.groups[0]], medians[rule.groups[-1]]
    inverted = [
        [low, high]
        for i, low in enumerate(rule.groups)
        for high in rule.groups[i + 1 :]
        if medians[low] > medians[high] * slack
    ]
    return {"medians": medians, "inverted": inverted, "holds": first < last and not inverted}


_JUDGES = {"count": _count, "ratio": _ratio, "order": _order}


def _describe(rule: Rule) -> str:
    low, high = rule.bounds()
    if rule.kind == "count":
        target = "the most common" if rule.target is MODE else repr(rule.target)
        span = f"{low}" if low == high else f"{low} to {high}"
        return f"{span} of {rule.n} samples equal {target}"
    if rule.kind == "ratio":
        a, b = rule.groups
        upper = "" if high is None else f" and {high}"
        return f"median {a} / median {b} between {low}{upper}"
    order = " < ".join(rule.groups)
    return f"medians in the order {order}, within a factor {rule.to_dict()['slack']}"


def _finite(value: Any) -> bool:
    """A number a bound or a median can be: an integer or a finite float, never a boolean."""
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


def _key(value: Any) -> str:
    """What makes two reduced samples equal: 1 and 1.0 are one value, 1 and true are not."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return json.dumps(value, sort_keys=True)


def _reduce(value: Any) -> Any:
    """A JSON scalar for a sample: finite numbers, booleans and null as themselves, a number
    that is not finite as its name (strict JSON has no NaN or Infinity), else a digest."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def unfalsifiable(rule: Rule) -> str | None:
    """Why no set of n samples can fail `rule`, or None when some set can.

    A count of the most common value is always between 1 and n, and a count of a named value
    between 0 and n, so a count rule whose bounds admit that whole range checks nothing. A
    ratio fails on a zero median below the line and an order on equal medians, whatever their
    bounds.
    """
    if rule.kind != "count":
        return None
    low, high = rule.bounds()
    least = 1 if rule.target is MODE else 0
    if low > least or high < rule.n:
        return None
    if least == rule.n:
        return "the most common of 1 sample is always that sample; set n to at least 2"
    return f"any {rule.n} samples satisfy it; set min above {least} or max below {rule.n}"


def resolve(default: Rule, override: Any, where: str) -> Rule:
    """The case's default rule with the profile's entry (n and bounds) applied.

    A key the entry leaves unset keeps the case's default. A count entry that changes n states
    min or max too: they count samples, so a default written for one n says nothing about
    another. An entry that contradicts a kept default, or whose rule no sample set can fail,
    is refused.
    """
    if override is None:
        return default
    given = {k: v for k, v in dataclasses.asdict(override).items() if v is not None}
    if default.kind == "count" and "n" in given and not {"min", "max"} & set(given):
        raise ProfileError(f"{where}: a count rule that sets n also sets min or max")
    kept = [
        f"{name} {getattr(default, name)}"
        for name in ("n", *BOUNDS[default.kind])
        if name not in given and getattr(default, name) is not None
    ]
    try:
        rule = dataclasses.replace(default, **given)
    except ValueError as exc:
        defaults = f"; a key left unset keeps the case's default ({', '.join(kept)})"
        raise ProfileError(f"{where}: {exc}{defaults if kept else ''}") from None
    why = unfalsifiable(rule)
    if why:
        raise ProfileError(f"{where}: {_describe(rule)} can never fail: {why}")
    return dataclasses.replace(rule, from_profile=rule.to_dict() != default.to_dict())
