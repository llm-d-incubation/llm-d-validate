"""Fleet invariants a profile declares: configuration reads, metrics and log signatures.

Some user-facing failures are invisible on the wire and plain in a fleet's configuration, its
metrics or its logs: a setting that turns on full error stacks, two routers active at once, a
router hashing KV blocks at a different size than the engine. These declarations name what to
read and what must hold. llmdv holds no fleet-specific object names, metric names or log text;
all of it comes from the profile.

Kept apart from `schema.py` so the parser and its vocabulary live in one place, and a profile
without a `fleet` block parses exactly as it always did.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, is_dataclass
from types import NoneType
from typing import Any, get_args, get_type_hints

# What a configuration rule checks. Each is one llmdv case, and each declared rule a variant.
CONFIG_CHECKS = (
    "settings",
    "values_agree",
    "field_values",
    "ready_endpoints",
    "selector_exclusive",
)
# How an object is read as text. `containers` is every container of every pod template in the
# object, as its command line plus one NAME=value line per environment variable. `data` is a
# ConfigMap's data values.
CONFIG_VIEWS = ("containers", "data")


class FleetDeclarationError(ValueError):
    """A fleet declaration that cannot be turned into a rule."""


@dataclass(frozen=True)
class ObjectRef:
    """Which Kubernetes objects a rule reads, through `kubectl get` only."""

    kind: str
    name: str | None = None
    selector: str | None = None
    namespace: str | None = None

    def describe(self) -> str:
        what = self.name or (f"-l {self.selector}" if self.selector else "(all)")
        return f"{self.kind} {what}"


@dataclass(frozen=True)
class Extraction:
    """One value read out of an object's text by a pattern with one capture group."""

    objects: ObjectRef
    pattern: str
    view: str = "containers"
    containers: tuple[str, ...] = ()
    # The value a unit holds when the pattern does not match it, such as an engine default.
    default: str | None = None


@dataclass(frozen=True)
class ConfigRule:
    id: str
    check: str
    objects: ObjectRef | None = None
    view: str = "containers"
    containers: tuple[str, ...] = ()
    require: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()
    sources: tuple[Extraction, ...] = ()
    expect: str | None = None
    expect_capability: str | None = None
    path: tuple[str, ...] = ()
    equals: Any = None
    at_least_seconds: float | None = None
    service: str | None = None
    count: int | None = None
    selector_path: tuple[str, ...] = ()
    # Run only where a selected ingress declares this capability, e.g. `cache_reuse`.
    when: str | None = None
    # The ingress this rule protects, for the result's ingress column. Optional.
    ingress: str | None = None


@dataclass(frozen=True)
class Signature:
    """A log line that must not appear in a source's run-window logs."""

    id: str
    pattern: str


def parse_signatures(raw: Any, where: str) -> tuple[Signature, ...]:
    """`forbid` on a log source: a list of {id, pattern}."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise FleetDeclarationError(f"{where}: expected a list of {{id, pattern}}")
    out = []
    for index, item in enumerate(raw):
        at = f"{where}[{index}]"
        item = _mapping(item, at)
        _known(item, {"id", "pattern"}, at)
        pattern = _str(item.get("pattern"), f"{at}.pattern", required=True) or ""
        try:
            re.compile(pattern)
        except re.error as exc:
            raise FleetDeclarationError(f"{at}.pattern: invalid pattern: {exc}") from None
        out.append(Signature(_str(item.get("id"), f"{at}.id", required=True) or "", pattern))
    if len({item.id for item in out}) != len(out):
        raise FleetDeclarationError(f"{where}: signature ids must be distinct")
    return tuple(out)


def parse_allow(raw: Any, where: str) -> tuple[str, ...]:
    """`allow` on a log source: patterns that excuse a line a signature matched."""
    return _patterns(raw, where)


# What a metrics rule checks. Each is one llmdv case, and each declared rule a variant.
METRIC_CHECKS = ("bounds", "agree", "spread", "header_label")
METRIC_RELATIONS = ("equal", "at_least")
# The placeholder a header_label rule's headers and query share, replaced per run.
NONCE = "{nonce}"


@dataclass(frozen=True)
class MetricsSource:
    """A Prometheus-compatible HTTP API, read with GET /api/v1/query only."""

    url: str
    auth: str = "none"  # none | bearer
    key_env: str | None = None
    proxy: str = "bypass"  # bypass | allow
    timeout: float = 15.0
    tls_verify: bool = True


@dataclass(frozen=True)
class MetricRule:
    id: str
    check: str
    query: str | None = None
    min: float | None = None
    max: float | None = None
    # A counter that only appears once incremented reads as zero when absent. Off by default:
    # an empty result is otherwise the commonest way a metrics check passes a broken fleet.
    absent_is_zero: bool = False
    left: str | None = None
    right: str | None = None
    absolute_tolerance: float | None = None
    relative_tolerance: float | None = None
    relation: str = "equal"
    label: str | None = None
    min_ratio: float | None = None
    min_series: int = 1
    headers: tuple[tuple[str, str], ...] = ()
    expect: str | None = None
    timeout_s: float = 120.0
    interval_s: float = 5.0
    when: str | None = None
    ingress: str | None = None


@dataclass(frozen=True)
class FleetChecks:
    config: tuple[ConfigRule, ...] = ()
    metrics: tuple[MetricRule, ...] = ()

    def config_rules(self, check: str) -> tuple[ConfigRule, ...]:
        return tuple(rule for rule in self.config if rule.check == check)

    def metric_rules(self, check: str) -> tuple[MetricRule, ...]:
        return tuple(rule for rule in self.metrics if rule.check == check)


# --------------------------------------------------------------------- parsing


def _mapping(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FleetDeclarationError(f"{where}: expected a mapping, got {raw!r}")
    return raw


def _known(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise FleetDeclarationError(f"{where}: unknown field(s) {', '.join(unknown)}")


def _str(value: Any, where: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise FleetDeclarationError(f"{where}: required")
        return None
    if not isinstance(value, str) or not value:
        raise FleetDeclarationError(f"{where}: expected a non-empty string, got {value!r}")
    return value


def _strs(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise FleetDeclarationError(f"{where}: expected a list of non-empty strings")
    return tuple(value)


def _patterns(value: Any, where: str) -> tuple[str, ...]:
    patterns = _strs(value, where)
    for index, pattern in enumerate(patterns):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise FleetDeclarationError(f"{where}[{index}]: invalid pattern: {exc}") from None
    return patterns


def _path(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise FleetDeclarationError(f"{where}: expected a non-empty list of keys")
    return tuple(str(item) for item in value)


def _objects(raw: Any, where: str) -> ObjectRef:
    raw = _mapping(raw, where)
    _known(raw, {"kind", "name", "selector", "namespace"}, where)
    ref = ObjectRef(
        kind=_str(raw.get("kind"), f"{where}.kind", required=True) or "",
        name=_str(raw.get("name"), f"{where}.name"),
        selector=_str(raw.get("selector"), f"{where}.selector"),
        namespace=_str(raw.get("namespace"), f"{where}.namespace"),
    )
    if ref.name and ref.selector:
        raise FleetDeclarationError(f"{where}: declare exactly one of name and selector")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", ref.kind):
        raise FleetDeclarationError(
            f"{where}.kind: expected one resource kind without ',', '/' or a leading '-'"
        )
    if ref.kind.lower().split(".", 1)[0] in ("secret", "secrets"):
        raise FleetDeclarationError(f"{where}.kind: rules never read Secrets")
    return ref


def _view(raw: dict[str, Any], where: str) -> str:
    view = raw.get("view", "containers")
    if view not in CONFIG_VIEWS:
        raise FleetDeclarationError(f"{where}.view: expected one of {list(CONFIG_VIEWS)}")
    return view


def _extraction(raw: Any, where: str) -> Extraction:
    raw = _mapping(raw, where)
    _known(raw, {"objects", "pattern", "view", "containers", "default"}, where)
    pattern = _str(raw.get("pattern"), f"{where}.pattern", required=True) or ""
    try:
        groups = re.compile(pattern).groups
    except re.error as exc:
        raise FleetDeclarationError(f"{where}.pattern: invalid pattern: {exc}") from None
    if groups != 1:
        raise FleetDeclarationError(f"{where}.pattern: needs exactly one capture group")
    default = raw.get("default")
    return Extraction(
        objects=_objects(raw.get("objects"), f"{where}.objects"),
        pattern=pattern,
        view=_view(raw, where),
        containers=_strs(raw.get("containers"), f"{where}.containers"),
        default=None if default is None else str(default),
    )


_CONFIG_FIELDS = {
    "settings": {"objects", "view", "containers", "require", "forbid"},
    "values_agree": {"sources", "expect", "expect_capability"},
    "field_values": {"objects", "path", "equals", "at_least_seconds"},
    "ready_endpoints": {"service", "count", "namespace"},
    "selector_exclusive": {"objects", "selector_path"},
}
_COMMON = {"id", "check", "when", "ingress"}


def _config_rule(raw: Any, where: str) -> ConfigRule:
    raw = _mapping(raw, where)
    rule_id = _str(raw.get("id"), f"{where}.id", required=True) or ""
    where = f"{where}({rule_id})"
    check = raw.get("check")
    if check not in CONFIG_CHECKS:
        raise FleetDeclarationError(f"{where}.check: expected one of {list(CONFIG_CHECKS)}")
    _known(raw, _COMMON | _CONFIG_FIELDS[check], where)
    common = {
        "id": rule_id,
        "check": check,
        "when": _str(raw.get("when"), f"{where}.when"),
        "ingress": _str(raw.get("ingress"), f"{where}.ingress"),
    }
    if check == "settings":
        require = _patterns(raw.get("require"), f"{where}.require")
        forbid = _patterns(raw.get("forbid"), f"{where}.forbid")
        if not (require or forbid):
            raise FleetDeclarationError(f"{where}: declare require or forbid patterns")
        return ConfigRule(
            **common,
            objects=_objects(raw.get("objects"), f"{where}.objects"),
            view=_view(raw, where),
            containers=_strs(raw.get("containers"), f"{where}.containers"),
            require=require,
            forbid=forbid,
        )
    if check == "values_agree":
        sources = raw.get("sources")
        if not isinstance(sources, list) or not sources:
            raise FleetDeclarationError(f"{where}.sources: expected a non-empty list")
        expect = raw.get("expect")
        if len(sources) < 2 and expect is None and raw.get("expect_capability") is None:
            raise FleetDeclarationError(
                f"{where}: one source needs expect or expect_capability to agree with"
            )
        return ConfigRule(
            **common,
            sources=tuple(
                _extraction(item, f"{where}.sources[{index}]") for index, item in enumerate(sources)
            ),
            expect=None if expect is None else str(expect),
            expect_capability=_str(raw.get("expect_capability"), f"{where}.expect_capability"),
        )
    if check == "field_values":
        equals = raw.get("equals")
        least = raw.get("at_least_seconds")
        if (equals is None) == (least is None):
            raise FleetDeclarationError(f"{where}: declare exactly one of equals, at_least_seconds")
        if least is not None and (type(least) not in (int, float) or least <= 0):
            raise FleetDeclarationError(f"{where}.at_least_seconds: expected a positive number")
        return ConfigRule(
            **common,
            objects=_objects(raw.get("objects"), f"{where}.objects"),
            path=_path(raw.get("path"), f"{where}.path"),
            equals=equals,
            at_least_seconds=None if least is None else float(least),
        )
    if check == "ready_endpoints":
        count = raw.get("count")
        if type(count) is not int or count < 0:
            raise FleetDeclarationError(f"{where}.count: expected a non-negative integer")
        service = _str(raw.get("service"), f"{where}.service", required=True)
        return ConfigRule(
            **common,
            objects=ObjectRef(
                kind="endpoints",
                name=service,
                namespace=_str(raw.get("namespace"), f"{where}.namespace"),
            ),
            service=service,
            count=count,
        )
    objects = _objects(raw.get("objects"), f"{where}.objects")
    if not objects.name:
        raise FleetDeclarationError(f"{where}.objects.name: required for selector_exclusive")
    return ConfigRule(
        **common,
        objects=objects,
        selector_path=_path(raw.get("selector_path"), f"{where}.selector_path"),
    )


def _number(value: Any, where: str) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise FleetDeclarationError(f"{where}: expected a number, got {value!r}")
    return float(value)


def _flag(value: Any, default: bool, where: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise FleetDeclarationError(f"{where}: expected true or false, got {value!r}")
    return value


_METRIC_FIELDS = {
    "bounds": {"query", "min", "max", "absent_is_zero"},
    "agree": {"left", "right", "absolute_tolerance", "relative_tolerance", "relation"},
    "spread": {"query", "label", "min_ratio", "min_series"},
    "header_label": {"query", "headers", "label", "expect", "timeout_s", "interval_s"},
}


def _metric_rule(raw: Any, where: str) -> MetricRule:
    raw = _mapping(raw, where)
    rule_id = _str(raw.get("id"), f"{where}.id", required=True) or ""
    where = f"{where}({rule_id})"
    check = raw.get("check")
    if check not in METRIC_CHECKS:
        raise FleetDeclarationError(f"{where}.check: expected one of {list(METRIC_CHECKS)}")
    _known(raw, _COMMON | _METRIC_FIELDS[check], where)
    common = {
        "id": rule_id,
        "check": check,
        "when": _str(raw.get("when"), f"{where}.when"),
        "ingress": _str(raw.get("ingress"), f"{where}.ingress"),
    }
    if check == "bounds":
        low, high = _number(raw.get("min"), f"{where}.min"), _number(raw.get("max"), f"{where}.max")
        if low is None and high is None:
            raise FleetDeclarationError(f"{where}: declare min, max or both")
        if low is not None and high is not None and low > high:
            raise FleetDeclarationError(f"{where}: min is above max")
        return MetricRule(
            **common,
            query=_str(raw.get("query"), f"{where}.query", required=True),
            min=low,
            max=high,
            absent_is_zero=_flag(raw.get("absent_is_zero"), False, f"{where}.absent_is_zero"),
        )
    if check == "agree":
        absolute = _number(raw.get("absolute_tolerance"), f"{where}.absolute_tolerance")
        relative = _number(raw.get("relative_tolerance"), f"{where}.relative_tolerance")
        if (absolute is None) == (relative is None):
            raise FleetDeclarationError(
                f"{where}: declare exactly one of absolute_tolerance, relative_tolerance"
            )
        if (absolute if absolute is not None else relative) < 0:
            raise FleetDeclarationError(f"{where}: a tolerance cannot be negative")
        relation = raw.get("relation", "equal")
        if relation not in METRIC_RELATIONS:
            raise FleetDeclarationError(
                f"{where}.relation: expected one of {list(METRIC_RELATIONS)}"
            )
        return MetricRule(
            **common,
            left=_str(raw.get("left"), f"{where}.left", required=True),
            right=_str(raw.get("right"), f"{where}.right", required=True),
            absolute_tolerance=absolute,
            relative_tolerance=relative,
            relation=relation,
        )
    if check == "spread":
        ratio = _number(raw.get("min_ratio"), f"{where}.min_ratio")
        if ratio is None or not 0 < ratio <= 1:
            raise FleetDeclarationError(f"{where}.min_ratio: expected a number in (0, 1]")
        least = raw.get("min_series", 1)
        if type(least) is not int or least < 1:
            raise FleetDeclarationError(f"{where}.min_series: expected a positive integer")
        return MetricRule(
            **common,
            query=_str(raw.get("query"), f"{where}.query", required=True),
            label=_str(raw.get("label"), f"{where}.label"),
            min_ratio=ratio,
            min_series=least,
        )
    headers = _mapping(raw.get("headers") or {}, f"{where}.headers")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise FleetDeclarationError(f"{where}.headers: expected string names and values")
    query = _str(raw.get("query"), f"{where}.query", required=True) or ""
    if NONCE in query and not any(NONCE in value for value in headers.values()):
        raise FleetDeclarationError(f"{where}: the query uses {NONCE} but no header carries it")
    if not common["ingress"]:
        raise FleetDeclarationError(f"{where}.ingress: required for header_label")
    timeout = _number(raw.get("timeout_s"), f"{where}.timeout_s")
    interval = _number(raw.get("interval_s"), f"{where}.interval_s")
    if timeout is not None and timeout <= 0:
        raise FleetDeclarationError(f"{where}.timeout_s: expected a positive number")
    if interval is not None and interval <= 0:
        raise FleetDeclarationError(f"{where}.interval_s: expected a positive number")
    return MetricRule(
        **common,
        query=query,
        headers=tuple(sorted(headers.items())),
        label=_str(raw.get("label"), f"{where}.label", required=True),
        expect=_str(
            None if raw.get("expect") is None else str(raw["expect"]),
            f"{where}.expect",
            required=True,
        ),
        timeout_s=120.0 if timeout is None else timeout,
        interval_s=5.0 if interval is None else interval,
    )


def parse_metrics_source(raw: Any) -> MetricsSource | None:
    """`environment.metrics`, or None when the profile declares no metrics source."""
    if raw is None:
        return None
    where = "environment.metrics"
    raw = _mapping(raw, where)
    _known(raw, {"url", "auth", "proxy", "timeout", "tls_verify"}, where)
    url = _str(raw.get("url"), f"{where}.url", required=True) or ""
    if not url.startswith(("http://", "https://")):
        raise FleetDeclarationError(f"{where}.url: must start with http:// or https://")
    auth = _mapping(raw.get("auth") or {}, f"{where}.auth")
    _known(auth, {"type", "key_env"}, f"{where}.auth")
    kind = auth.get("type", "none")
    if kind not in ("none", "bearer"):
        raise FleetDeclarationError(f"{where}.auth.type: expected none or bearer")
    key_env = _str(auth.get("key_env"), f"{where}.auth.key_env")
    if kind == "bearer" and not key_env:
        raise FleetDeclarationError(f"{where}.auth: bearer requires key_env")
    proxy = raw.get("proxy", "bypass")
    if proxy not in ("bypass", "allow"):
        raise FleetDeclarationError(f"{where}.proxy: expected bypass or allow")
    timeout = _number(raw.get("timeout"), f"{where}.timeout")
    if timeout is not None and timeout <= 0:
        raise FleetDeclarationError(f"{where}.timeout: expected a positive number")
    return MetricsSource(
        url=url.rstrip("/"),
        auth=kind,
        key_env=key_env if kind == "bearer" else None,
        proxy=proxy,
        timeout=15.0 if timeout is None else timeout,
        tls_verify=_flag(raw.get("tls_verify"), True, f"{where}.tls_verify"),
    )


def _rules(raw: dict[str, Any], key: str, parse) -> tuple:
    items = raw.get(key) or []
    if not isinstance(items, list):
        raise FleetDeclarationError(f"fleet.{key}: expected a list of rules")
    rules = tuple(parse(item, f"fleet.{key}[{index}]") for index, item in enumerate(items))
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise FleetDeclarationError(f"fleet.{key}: duplicate rule id {rule.id!r}")
        seen.add(rule.id)
    return rules


def parse_fleet(raw: Any) -> FleetChecks:
    """The `fleet` block, or empty checks when the profile has none."""
    if raw is None:
        return FleetChecks()
    raw = _mapping(raw, "fleet")
    _known(raw, {"config", "metrics"}, "fleet")
    return FleetChecks(
        config=_rules(raw, "config", _config_rule),
        metrics=_rules(raw, "metrics", _metric_rule),
    )


def fleet_diagnostics(profile: Any) -> list[tuple[str, str, str]]:
    """Cross-section checks on the fleet block, as (level, path, message) triples.

    Offline like the rest of `validate`: names are checked against the profile, never against a
    cluster.
    """
    from dataclasses import fields as dataclass_fields

    from .schema import Capabilities

    def capability_path_known(path: str) -> bool:
        current: type = Capabilities
        for index, part in enumerate(path.split(".")):
            hints = get_type_hints(current)
            if part not in hints:
                return False
            choices = [item for item in get_args(hints[part]) if item is not NoneType]
            next_type = choices[0] if len(choices) == 1 else hints[part]
            if index < len(path.split(".")) - 1:
                if not isinstance(next_type, type) or not is_dataclass(next_type):
                    return False
                current = next_type
        return True

    out: list[tuple[str, str, str]] = []
    capabilities = {item.name for item in dataclass_fields(Capabilities)}
    ingress_ids = set(profile.ingress_ids)
    namespace = profile.environment.namespace
    if profile.fleet.metrics and profile.environment.metrics is None:
        out.append(("error", "fleet.metrics", "declares rules but environment.metrics is unset"))
    for rule in profile.fleet.metrics:
        at = f"fleet.metrics({rule.id})"
        if rule.ingress and rule.ingress not in ingress_ids:
            out.append(("error", f"{at}.ingress", f"names undeclared ingress {rule.ingress!r}"))
        if rule.when and rule.when not in capabilities:
            out.append(("error", f"{at}.when", f"unknown capability {rule.when!r}"))
    for rule in profile.fleet.config:
        at = f"fleet.config({rule.id})"
        gated_out = bool(rule.when) and not any(
            getattr(ingress.capabilities, rule.when, None) for ingress in profile.ingresses
        )
        if rule.ingress and rule.ingress not in ingress_ids:
            out.append(("error", f"{at}.ingress", f"names undeclared ingress {rule.ingress!r}"))
        if rule.when and rule.when not in capabilities:
            out.append(("error", f"{at}.when", f"unknown capability {rule.when!r}"))
        if rule.expect_capability:
            if not capability_path_known(rule.expect_capability):
                out.append(
                    (
                        "error",
                        f"{at}.expect_capability",
                        f"unknown capability {rule.expect_capability!r}",
                    )
                )
            elif not gated_out and not any(
                _capability_value(ingress.capabilities, rule.expect_capability) is not None
                for ingress in profile.ingresses
            ):
                out.append(
                    (
                        "error",
                        f"{at}.expect_capability",
                        f"capability {rule.expect_capability!r} is not declared by any ingress",
                    )
                )
        refs = [rule.objects] if rule.objects else [source.objects for source in rule.sources]
        if not namespace and any(ref is not None and not ref.namespace for ref in refs):
            out.append(("error", at, "reads objects with no namespace; set environment.namespace"))
    return out


def _capability_value(capabilities: Any, path: str) -> Any:
    node = capabilities
    for part in path.split("."):
        node = getattr(node, part, None)
        if node is None:
            return None
    return node
