"""YAML -> Profile, with diagnostics that name the path that is wrong.

Validation collects every problem rather than raising on the first, because a profile is
edited as a whole and a loader that reports one error per run wastes a round trip each time.
Like a kustomize validator, it reports exactly where a profile breaks.

`validate` is offline by design. It resolves no secrets and contacts no cluster, so a profile
can be written and checked on a laptop with no access to the fleet it describes. Missing
credentials and unreachable endpoints are preflight's job, and keeping them there is what
makes the distinction between "your profile is wrong" (exit 2) and "your vantage point is
wrong" (exit 3) meaningful.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

from ..surfaces import base as surfaces
from . import evolution
from . import transport as transport_contract
from .schema import (
    CACHE_AFFINITIES,
    CHAT_FIELDS,
    CHAT_FORCED_TOOL_FINISHES,
    CHAT_OPTIONS,
    CHAT_REASONING_EFFORTS,
    CHAT_REPEATS,
    CLAUDE_CODE_MIN_VERSION,
    CLIENT_SESSIONS,
    COMPLETIONS_FIELDS,
    EFFORT_VALUES,
    ERROR_CLASSES,
    FEATURE_DISPOSITIONS,
    FIELD_DISPOSITIONS,
    MESSAGES_CHECKS,
    MESSAGES_EFFORTS,
    MESSAGES_FEATURE_OUTCOMES,
    MESSAGES_FEATURES,
    MESSAGES_MIN_THINKING_BUDGET,
    MESSAGES_THINKING_MODES,
    RESPONSES_FEATURES,
    RESPONSES_OPTIONS,
    RESPONSES_REASONING_EFFORTS,
    RESPONSES_REFERENCE_FEATURES,
    SCENARIO_CHECKS,
    SETTING_VALUES,
    TOOL_STRICT_LEVELS,
    UNSET_EFFORT,
    Auth,
    AuthType,
    CacheReuse,
    Capabilities,
    Client,
    ClientBinary,
    Disposition,
    EffortRendering,
    Environment,
    FleetPolicy,
    Ingress,
    IngressKind,
    KnownFailure,
    LogSource,
    Model,
    Profile,
    ProxyMode,
    Reasoning,
    Requirement,
    Setup,
    Timeouts,
    Tls,
    Tools,
    Vantage,
    parse_setting,
)

SUPPORTED_SCHEMA = "llmdv.profile/1"


class ProfileError(Exception):
    """Raised for a profile that cannot be turned into an object at all."""


@dataclass(frozen=True)
class Diagnostic:
    level: str  # "error" | "warning"
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.level.upper():<7} {self.path}: {self.message}"


def load(path: str | Path) -> Profile:
    """Parse a profile file into a Profile. Raises ProfileError on unusable input."""
    file_path = Path(path)
    if not file_path.is_file():
        raise ProfileError(f"no such profile file: {file_path}")
    try:
        raw = yaml.safe_load(file_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ProfileError(f"{file_path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"{file_path}: top level must be a mapping")
    return _build(raw, file_path)


def validate(profile: Profile) -> list[Diagnostic]:
    """Offline checks across the whole profile. Errors block a run; warnings do not."""
    from .fleet import fleet_diagnostics

    out = validate_models(profile)

    if profile.schema != SUPPORTED_SCHEMA:
        out.append(
            Diagnostic(
                "error",
                "schema",
                f"unsupported schema {profile.schema!r}, expected {SUPPORTED_SCHEMA!r}",
            )
        )
    if not profile.ingresses:
        out.append(Diagnostic("error", "ingresses", "at least one ingress is required"))

    seen: set[str] = set()
    for index, ingress in enumerate(profile.ingresses):
        at = f"ingresses[{index}]({ingress.id})"
        if ingress.id in seen:
            out.append(Diagnostic("error", at, f"duplicate ingress id {ingress.id!r}"))
        seen.add(ingress.id)

        if ingress.base_url_env and not ingress.base_url:
            pass  # Unset here is not a profile error: preflight names the variable to export.
        elif not ingress.base_url.startswith(("http://", "https://")):
            out.append(
                Diagnostic(
                    "error",
                    f"{at}.base_url",
                    f"must start with http:// or https://, got {ingress.base_url!r}",
                )
            )
        if ingress.auth.type is not AuthType.NONE and not ingress.auth.key_env:
            out.append(
                Diagnostic(
                    "error", f"{at}.auth", f"auth type {ingress.auth.type.value!r} requires key_env"
                )
            )
        if not ingress.surfaces:
            out.append(
                Diagnostic(
                    "warning",
                    f"{at}.surfaces",
                    "no surfaces declared, so every case here will skip",
                )
            )

        for surface in ingress.surfaces:
            if surface not in surfaces.KNOWN_SURFACES:
                out.append(
                    Diagnostic(
                        "error",
                        f"{at}.surfaces",
                        f"unknown surface {surface!r}; known: "
                        f"{', '.join(sorted(surfaces.KNOWN_SURFACES))}",
                    )
                )
            elif surface not in surfaces.implemented():
                # A warning, not an error: a profile may honestly describe a fleet before llmdv
                # can drive every surface it exposes. These cases skip as `not_implemented`.
                out.append(
                    Diagnostic(
                        "warning",
                        f"{at}.surfaces",
                        f"surface {surface!r} is declared but not implemented in "
                        "this build; its cases will skip as not_implemented",
                    )
                )

        if not ingress.tls.verify:
            out.append(
                Diagnostic(
                    "warning",
                    f"{at}.tls.verify",
                    "TLS certificates are not verified for this ingress",
                )
            )

        reuse = ingress.capabilities.cache_reuse
        for peer in reuse.shared_with if reuse else ():
            if peer not in profile.ingress_ids:
                out.append(
                    Diagnostic(
                        "error",
                        f"{at}.capabilities.cache_reuse.shared_with",
                        f"names no declared ingress: {peer!r}",
                    )
                )

        for error_class in ingress.error_status:
            if error_class not in ERROR_CLASSES:
                out.append(
                    Diagnostic(
                        "error", f"{at}.error_status", f"unknown error class {error_class!r}"
                    )
                )
        # A declared image_input status states the model is text-only, so it and vision
        # cannot both be true of one ingress.
        if ingress.capabilities.vision and "image_input" in ingress.error_status:
            out.append(
                Diagnostic(
                    "error",
                    f"{at}.error_status",
                    "image_input declares a text-only model, but capabilities.vision is set",
                )
            )
        for error_class in ingress.unstructured_errors:
            if error_class not in ERROR_CLASSES:
                out.append(
                    Diagnostic(
                        "error", f"{at}.unstructured_errors", f"unknown error class {error_class!r}"
                    )
                )

        for req_index, requirement in enumerate(ingress.setup.requires):
            req_at = f"{at}.setup.requires[{req_index}]"
            if not (requirement.target or requirement.selector):
                out.append(Diagnostic("error", req_at, "needs either target or selector"))
            if requirement.kind == "port_forward" and not requirement.local_port:
                out.append(Diagnostic("error", req_at, "port_forward needs local_port"))

    # Cross-section consistency. A client needing a surface no ingress exposes is dead
    # config: it can never run, and nothing at runtime notices.
    exposed = {surface for ingress in profile.ingresses for surface in ingress.surfaces}
    for index, client in enumerate(profile.clients):
        at = f"clients[{index}]({client.id})"
        if not client.surfaces:
            out.append(
                Diagnostic("warning", at, "declares no surfaces, so --client selects nothing")
            )
        for surface in client.surfaces:
            if surface not in exposed:
                out.append(
                    Diagnostic(
                        "error",
                        f"{at}.surfaces",
                        f"needs {surface!r}, which no ingress in this profile exposes",
                    )
                )

    for header, per_ingress in profile.headers.items():
        for ingress_id in per_ingress:
            if ingress_id not in seen:
                out.append(
                    Diagnostic(
                        "error",
                        f"headers.{header}",
                        f"names ingress {ingress_id!r}, which is not declared",
                    )
                )

    for index, known in enumerate(profile.known_failures):
        at = f"known_failures[{index}]"
        for ingress_id in known.ingresses:
            if ingress_id not in seen:
                out.append(
                    Diagnostic("error", at, f"names ingress {ingress_id!r}, which is not declared")
                )
        if not known.cases and not known.ingresses:
            out.append(
                Diagnostic(
                    "warning",
                    at,
                    f"covers every case failing {known.check!r} at every ingress; scope it with "
                    "`cases` or `ingress` unless that is meant",
                )
            )

    out += [Diagnostic(*item) for item in fleet_diagnostics(profile)]
    return out


def validate_models(profile: Profile) -> list[Diagnostic]:
    """Model rules shared by offline validation and native pytest before any requests."""
    models = [("model", profile.model)] + [
        (f"ingresses({ingress.id}).model", ingress.model) for ingress in profile.ingresses
    ]
    out = []
    for where, model in models:
        if not model.public_id:
            out.append(Diagnostic("error", f"{where}.public_id", "required"))
        if model.max_model_len is not None and model.max_model_len < 0:
            out.append(Diagnostic("error", f"{where}.max_model_len", "must not be negative"))
        if model.reasoning.is_separate_field and model.reasoning.is_inline:
            out.append(
                Diagnostic(
                    "warning",
                    f"{where}.reasoning",
                    "both field and inline_tag are set; the field is preferred when present",
                )
            )
    return out


def resolve_secret(ingress: Ingress) -> str | None:
    """Read an ingress's credential from its declared environment variable.

    Returns None when unset. Deliberately not an error here: `validate` stays offline and
    secret-free, and preflight decides whether a missing credential blocks the run -- which
    depends on whether that ingress was actually selected.
    """
    if ingress.auth.key_env is None:
        return None
    return os.environ.get(ingress.auth.key_env) or None


def resolve_all_secrets(profile: Profile) -> dict[str, str]:
    """Resolved credentials by ingress id, for ingresses that have one set."""
    found: dict[str, str] = {}
    for ingress in profile.ingresses:
        secret = resolve_secret(ingress)
        if secret:
            found[ingress.id] = secret
    return found


# --------------------------------------------------------------------- building


def _build(raw: dict[str, Any], path: Path) -> Profile:
    try:
        model_raw = raw.get("model") or {}
        capabilities_raw = raw.get("capabilities") or {}
        model = _model(model_raw)
        capabilities = _capabilities(capabilities_raw)
        return Profile(
            schema=str(raw.get("schema", SUPPORTED_SCHEMA)),
            name=str(raw.get("name") or path.stem),
            vantage=_enum(Vantage, raw.get("vantage"), Vantage.EXTERNAL, "vantage"),
            environment=_environment(raw.get("environment") or {}),
            model=model,
            ingresses=tuple(
                _ingress(item, model_raw, capabilities_raw) for item in (raw.get("ingresses") or [])
            ),
            capabilities=capabilities,
            headers=_headers(raw.get("headers") or {}),
            clients=tuple(_client(item, path.parent) for item in (raw.get("clients") or [])),
            fleet_policy=_enum(
                FleetPolicy, raw.get("fleet_policy"), FleetPolicy.DEDICATED, "fleet_policy"
            ),
            source_path=path,
            fleet=_fleet(raw.get("fleet")),
            known_failures=_known_failures(raw.get("known_failures")),
        )
    except ProfileError:
        raise
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProfileError(f"{path}: {exc}") from exc


_ISSUE = re.compile(r"^(?:[\w.-]+/[\w.-]+)?#\d+$|^https?://\S+$")


def _known_failures(raw: Any) -> tuple[KnownFailure, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProfileError(f"known_failures: expected a list, got {raw!r}")
    out = []
    for index, item in enumerate(raw):
        where = f"known_failures[{index}]"
        if not isinstance(item, dict):
            raise ProfileError(f"{where}: expected a mapping, got {item!r}")
        unknown = set(item) - {"check", "issue", "cases", "ingress", "fix", "until"}
        if unknown:
            raise ProfileError(f"{where}: unknown keys {sorted(unknown)}")
        check = _string(item.get("check", ""), f"{where}.check")
        if not re.search(r"[^*?]", check):
            # `*` alone would label every failure in the run, which is the one thing this
            # block must never be able to do.
            raise ProfileError(
                f"{where}.check: the first failing check is required, with at least one literal "
                f"character; got {check!r}"
            )
        issue = _string(item.get("issue", ""), f"{where}.issue")
        if not _ISSUE.match(issue):
            raise ProfileError(
                f"{where}.issue: expected 'owner/repo#N', '#N' or a URL, got {issue!r}"
            )
        until_raw = item.get("until") or {}
        if not isinstance(until_raw, dict):
            raise ProfileError(f"{where}.until: expected a mapping of signal to value")
        until = {}
        for name, value in until_raw.items():
            signal = _string(name, f"{where}.until").lower()
            if not isinstance(value, str) or not signal or not value:
                # YAML reads `1.100` as the float 1.1 and `1.97` as 1.97: a version must be
                # quoted, or the comparison is made against a number that is not the version.
                raise ProfileError(
                    f"{where}.until.{name}: expected a quoted version string, got {value!r}"
                )
            until[signal] = value
        out.append(
            KnownFailure(
                check=check,
                issue=issue,
                cases=_strings(item.get("cases") or [], f"{where}.cases"),
                ingresses=_one_or_more(item.get("ingress"), f"{where}.ingress"),
                fix=_string(item["fix"], f"{where}.fix") if item.get("fix") is not None else None,
                until=until,
            )
        )
    return tuple(out)


def _one_or_more(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return _strings(value, where)


def contract_digest(path: str | Path) -> str | None:
    """A short digest of the declarations in a profile file, or None when it cannot be read.

    Hashes the parsed document rather than its bytes, with `known_failures` left out: comments
    carry probes, and the known-failure list carries triage, and neither changes what a run
    asserts. A digest that moved with them would drop every earlier run from a comparison
    window (`compare/diff.py`) each time a finding was filed or a comment updated.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if isinstance(raw, dict):
        raw = {name: value for name, value in raw.items() if name != "known_failures"}
    canonical = json.dumps(_stringify_keys(raw), sort_keys=True, default=str, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def _stringify_keys(value: Any) -> Any:
    """Mapping keys as strings throughout, so `sort_keys` never compares an int with a str."""
    if isinstance(value, dict):
        return {str(k): _stringify_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stringify_keys(v) for v in value]
    return value


def _bool(value: Any, default: bool, where: str) -> bool:
    """A declared boolean, rejecting anything that is not one.

    `bool()` coerces, and that is the wrong behaviour for a declaration: every non-empty string is
    truthy, so `rate_limited: "false"` and `count_tokens: "off"` both read as True. YAML only
    produces a string here when the value was quoted or misspelled, and in both cases the author
    meant something they did not get.

    It matters most where a declaration RELAXES something -- `rate_limited: true` turns a 429 from
    a failure into a skip -- because a typo would silently switch off a check rather than
    switching one on.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ProfileError(f"{where}: expected true or false, got {value!r}")


def _enum(enum_cls, value, default, where: str):
    if value is None:
        return default
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(member.value for member in enum_cls)
        raise ProfileError(f"{where}: {value!r} is not one of: {allowed}") from None


def _environment(raw: dict[str, Any]) -> Environment:
    kubeconfig = raw.get("kubeconfig")
    return Environment(
        cluster=_opt_str(raw.get("cluster")),
        context=_opt_str(raw.get("context")),
        namespace=_opt_str(raw.get("namespace")),
        kubeconfig=Path(str(kubeconfig)).expanduser() if kubeconfig else None,
        metrics=_metrics_source(raw.get("metrics")),
    )


def _fleet(raw: Any):
    from .fleet import FleetDeclarationError, parse_fleet

    try:
        return parse_fleet(raw)
    except FleetDeclarationError as exc:
        raise ProfileError(str(exc)) from None


def _metrics_source(raw: Any):
    from .fleet import FleetDeclarationError, parse_metrics_source

    try:
        return parse_metrics_source(raw)
    except FleetDeclarationError as exc:
        raise ProfileError(str(exc)) from None


def _check_override_keys(raw, cls, where, nullable=(), renames=None) -> None:
    """Overrides are partial mappings, but misspellings and invalid clears are errors."""
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: expected a mapping, got {raw!r}")
    names = {(renames or {}).get(item.name, item.name) for item in fields(cls)}
    for name, value in raw.items():
        if name not in names:
            raise ProfileError(f"{where}.{name}: unknown field")
        if value is None and name not in nullable:
            raise ProfileError(f"{where}.{name}: cannot be null")


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"{where}: expected a string, got {value!r}")
    return value


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ProfileError(f"{where}: expected a list of strings, got {value!r}")
    return tuple(_string(item, f"{where}[{index}]") for index, item in enumerate(value))


def _markup(value: Any, where: str) -> tuple[str, ...]:
    markup = _strings(value, where)
    for index, item in enumerate(markup):
        if not item:
            raise ProfileError(f"{where}[{index}]: an empty string matches every answer")
    return markup


def _https_url(value: Any, where: str) -> str | None:
    """An absolute https URL with a host, or None when undeclared."""
    if value is None:
        return None
    url = _string(value, where)
    if not re.match(r"^https://[^/\s?#]+(/\S*)?$", url):
        raise ProfileError(f"{where}: expected an https:// URL with a host, got {value!r}")
    return url


def _nullable_string(value: Any, where: str) -> str | None:
    return None if value is None else _string(value, where) or None


def _one_of(value: Any, allowed: tuple[str, ...], default: str, where: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ProfileError(f"{where}: expected one of {list(allowed)}, got {value!r}")
    return value


def _deep_merge(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge mappings without mutating either input; scalars, lists and null replace."""
    merged = dict(defaults)
    for name, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(name), dict):
            merged[name] = _deep_merge(merged[name], value)
        else:
            merged[name] = value
    return merged


def _ingress_model(override: Any, defaults: dict[str, Any], where: str) -> Model:
    _check_override_keys(override, Model, where, nullable=("max_model_len", "answer_max_tokens"))
    _check_override_keys(
        override.get("reasoning", {}),
        Reasoning,
        f"{where}.reasoning",
        nullable=("parser", "field", "inline_tag"),
        renames={"field_name": "field"},
    )
    _check_override_keys(override.get("tools", {}), Tools, f"{where}.tools", nullable=("parser",))
    return _model(_deep_merge(defaults, override), where)


def _ingress_capabilities(override: Any, defaults: dict[str, Any], where: str) -> Capabilities:
    _check_override_keys(
        override,
        Capabilities,
        where,
        nullable=(
            "cache_reuse",
            "effort_rendering",
            "model_info",
            "model_retrieval",
            "responses_input_tokens",
            "responses_compact",
        ),
    )
    return _capabilities(_deep_merge(defaults, override), where)


def _model(raw: dict[str, Any], where: str = "model") -> Model:
    reasoning_raw = raw.get("reasoning") or {}
    tools_raw = raw.get("tools") or {}
    limit = raw.get("max_model_len")
    if limit is not None and type(limit) is not int:
        raise ProfileError(f"{where}.max_model_len: expected an integer or null, got {limit!r}")
    return Model(
        public_id=_string(raw.get("public_id", ""), f"{where}.public_id"),
        aliases=_strings(raw.get("aliases", ()), f"{where}.aliases"),
        max_model_len=limit,
        answer_max_tokens=_opt_positive_int(
            raw.get("answer_max_tokens"), f"{where}.answer_max_tokens"
        ),
        reasoning=Reasoning(
            parser=_nullable_string(reasoning_raw.get("parser"), f"{where}.reasoning.parser"),
            field_name=_nullable_string(reasoning_raw.get("field"), f"{where}.reasoning.field"),
            inline_tag=_nullable_string(
                reasoning_raw.get("inline_tag"),
                f"{where}.reasoning.inline_tag",
            ),
            default_on=_bool(
                reasoning_raw.get("default_on"),
                False,
                f"{where}.reasoning.default_on",
            ),
        ),
        tools=Tools(
            parser=_nullable_string(tools_raw.get("parser"), f"{where}.tools.parser"),
            auto_choice=_bool(
                tools_raw.get("auto_choice"),
                False,
                f"{where}.tools.auto_choice",
            ),
            forced_finish_reason=_one_of(
                tools_raw.get("forced_finish_reason"),
                CHAT_FORCED_TOOL_FINISHES,
                "tool_calls",
                f"{where}.tools.forced_finish_reason",
            ),
            markup=_markup(tools_raw.get("markup", ()), f"{where}.tools.markup"),
            strict_level=_one_of(
                tools_raw.get("strict_level"),
                TOOL_STRICT_LEVELS,
                "auto",
                f"{where}.tools.strict_level",
            ),
        ),
    )


def _base_url(raw: dict[str, Any], ingress_id: str) -> str:
    """`base_url`, or the value of the variable `base_url_env` names. Never both."""
    env = _opt_str(raw.get("base_url_env"))
    if env and raw.get("base_url"):
        raise ProfileError(f"ingresses({ingress_id}): declare base_url or base_url_env, not both")
    value = os.environ.get(env, "") if env else str(raw.get("base_url") or "")
    return value.strip().rstrip("/")


def _ingress(
    raw: dict[str, Any], model_defaults: dict[str, Any], capability_defaults: dict[str, Any]
) -> Ingress:
    ingress_id = str(raw.get("id") or "")
    auth_raw = raw.get("auth") or {}
    timeouts_raw = raw.get("timeouts") or {}
    setup_raw = raw.get("setup") or {}
    ingress = Ingress(
        id=ingress_id,
        model=_ingress_model(
            raw.get("model", {}), model_defaults, f"ingresses({ingress_id}).model"
        ),
        capabilities=_ingress_capabilities(
            raw.get("capabilities", {}),
            capability_defaults,
            f"ingresses({ingress_id}).capabilities",
        ),
        kind=_enum(IngressKind, raw.get("kind"), IngressKind.VLLM, f"ingresses({ingress_id}).kind"),
        base_url=_base_url(raw, ingress_id),
        base_url_env=_opt_str(raw.get("base_url_env")),
        auth=Auth(
            type=_enum(
                AuthType, auth_raw.get("type"), AuthType.NONE, f"ingresses({ingress_id}).auth.type"
            ),
            key_env=_opt_str(auth_raw.get("key_env")),
        ),
        health_path=str(raw.get("health_path") or "/health"),
        surfaces=tuple(str(item) for item in (raw.get("surfaces") or [])),
        timeouts=Timeouts(
            connect=float(timeouts_raw.get("connect", 5.0)),
            request=float(timeouts_raw.get("request", 600.0)),
            stream_idle=float(timeouts_raw.get("stream_idle", 60.0)),
        ),
        error_status={
            str(key): tuple(int(code) for code in _as_list(value))
            for key, value in (raw.get("error_status") or {}).items()
        },
        unstructured_errors=tuple(str(item) for item in (raw.get("unstructured_errors") or [])),
        rate_limited=_bool(
            raw.get("rate_limited"), False, f"ingresses[{raw.get('id')}].rate_limited"
        ),
        logs=tuple(_log_source(item) for item in (raw.get("logs") or [])),
        tls=Tls(verify=_bool((raw.get("tls") or {}).get("verify"), True, "tls.verify")),
        transport=_transport(raw.get("transport"), f"ingresses({ingress_id}).transport"),
        setup=Setup(
            proxy=_enum(
                ProxyMode,
                setup_raw.get("proxy"),
                ProxyMode.BYPASS,
                f"ingresses({ingress_id}).setup.proxy",
            ),
            requires=tuple(_requirement(item) for item in (setup_raw.get("requires") or [])),
        ),
    )
    _check_http_redirect(ingress)
    return ingress


def _check_http_redirect(ingress: Ingress) -> None:
    """`transport.http_redirect` belongs on an ingress reached over TLS on the default port.

    Only a literal `base_url` is checked here. A value from `base_url_env` is the operator's
    environment rather than the profile, so preflight checks it and names the variable.
    """
    if ingress.transport.http_redirect is None or ingress.base_url_env:
        return
    reason = transport_contract.plain_http_url(ingress.base_url)[1]
    if reason:
        raise ProfileError(f"ingresses({ingress.id}): {reason}")


def _transport(raw: Any, where: str) -> transport_contract.TransportContract:
    try:
        return transport_contract.parse(raw, where)
    except transport_contract.TransportError as exc:
        raise ProfileError(str(exc)) from exc


def _requirement(raw: dict[str, Any]) -> Requirement:
    return Requirement(
        kind=str(raw.get("kind") or "port_forward"),
        local_port=_opt_int(raw.get("local_port")),
        remote_port=_opt_int(raw.get("remote_port")),
        namespace=_opt_str(raw.get("namespace")),
        target=_opt_str(raw.get("target")),
        selector=_opt_str(raw.get("selector")),
        command=_opt_str(raw.get("command")),
    )


def _log_source(raw: dict[str, Any]) -> LogSource:
    from .fleet import FleetDeclarationError, parse_allow, parse_signatures

    try:
        forbid = parse_signatures(raw.get("forbid"), "logs.forbid")
        allow = parse_allow(raw.get("allow"), "logs.allow")
    except FleetDeclarationError as exc:
        raise ProfileError(str(exc)) from None
    return LogSource(
        selector=_opt_str(raw.get("selector")),
        target=_opt_str(raw.get("target")),
        container=_opt_str(raw.get("container")),
        tail=int(raw.get("tail", 2000)),
        forbid=forbid,
        allow=allow,
    )


def _surface_capability(value: Any, where: str) -> bool | tuple[str, ...]:
    """A boolean or an explicit surface list, without truthiness coercion."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if not isinstance(value, list):
        raise ProfileError(f"{where}: expected true, false or a list of surfaces, got {value!r}")
    for index, name in enumerate(value):
        if not isinstance(name, str) or name not in surfaces.KNOWN_SURFACES:
            raise ProfileError(
                f"{where}[{index}]: unknown surface {name!r}; "
                f"known: {', '.join(sorted(surfaces.KNOWN_SURFACES))}"
            )
    return tuple(value)


def _declared_values(value: Any, allowed: tuple[str, ...], where: str) -> tuple[str, ...]:
    values = _strings(value, where)
    if any(item not in allowed for item in values) or len(values) != len(set(values)):
        raise ProfileError(
            f"{where}: expected distinct values from {sorted(allowed)}, got {value!r}"
        )
    return values


def _field_dispositions(value: Any, allowed: tuple[str, ...], where: str) -> dict[str, str]:
    """A mapping of known field probes to declared dispositions."""
    if not isinstance(value, dict):
        raise ProfileError(f"{where}: expected a mapping of field to disposition, got {value!r}")
    for name, disposition in value.items():
        if name not in allowed:
            raise ProfileError(f"{where}.{name}: unknown field probe; known: {', '.join(allowed)}")
        if disposition not in FIELD_DISPOSITIONS:
            raise ProfileError(
                f"{where}.{name}: expected one of {', '.join(FIELD_DISPOSITIONS)}, "
                f"got {disposition!r}"
            )
    return dict(value)


def _contract_surfaces(value: Any, where: str) -> bool | tuple[str, ...]:
    selected = _surface_capability(value, where)
    if isinstance(selected, tuple) and set(selected) - {"chat_completions", "completions"}:
        raise ProfileError(f"{where}: select chat_completions and/or completions")
    return selected


def _capabilities(raw: dict[str, Any], where: str = "capabilities") -> Capabilities:
    return Capabilities(
        prefix_stability=_surface_capability(
            raw.get("prefix_stability"), f"{where}.prefix_stability"
        ),
        count_tokens=_bool(raw.get("count_tokens"), False, f"{where}.count_tokens"),
        anthropic_betas=_strings(raw.get("anthropic_betas", ()), f"{where}.anthropic_betas"),
        messages_checks=_declared_values(
            raw.get("messages_checks", ()), MESSAGES_CHECKS, f"{where}.messages_checks"
        ),
        background_responses=_bool(
            raw.get("background_responses"), False, f"{where}.background_responses"
        ),
        stateful_responses=_bool(
            raw.get("stateful_responses"), False, f"{where}.stateful_responses"
        ),
        stateless_responses=_stateless_responses(raw, where),
        model_info=_model_info(raw.get("model_info") or {}, f"{where}.model_info"),
        model_retrieval=(
            None
            if raw.get("model_retrieval") is None
            else _bool(raw.get("model_retrieval"), False, f"{where}.model_retrieval")
        ),
        responses_compact=(
            None
            if raw.get("responses_compact") is None
            else _bool(raw.get("responses_compact"), False, f"{where}.responses_compact")
        ),
        responses_input_tokens=(
            None
            if raw.get("responses_input_tokens") is None
            else _bool(raw.get("responses_input_tokens"), False, f"{where}.responses_input_tokens")
        ),
        responses_features=_responses_features(
            raw.get("responses_features", {}), f"{where}.responses_features"
        ),
        structured_output=_surface_capability(
            raw.get("structured_output"), f"{where}.structured_output"
        ),
        reasoning_tokens=_surface_capability(
            raw.get("reasoning_tokens"), f"{where}.reasoning_tokens"
        ),
        vllm_options=_strings(raw.get("vllm_options", ()), f"{where}.vllm_options"),
        messages_thinking=_declared_values(
            raw.get("messages_thinking", ()), MESSAGES_THINKING_MODES, f"{where}.messages_thinking"
        ),
        messages_efforts=_declared_values(
            raw.get("messages_efforts", ()), MESSAGES_EFFORTS, f"{where}.messages_efforts"
        ),
        chat_reasoning_efforts=_declared_values(
            raw.get("chat_reasoning_efforts", ()),
            CHAT_REASONING_EFFORTS,
            f"{where}.chat_reasoning_efforts",
        ),
        responses_options=_declared_values(
            raw.get("responses_options", ()), RESPONSES_OPTIONS, f"{where}.responses_options"
        ),
        responses_reasoning_efforts=_declared_values(
            raw.get("responses_reasoning_efforts", ()),
            RESPONSES_REASONING_EFFORTS,
            f"{where}.responses_reasoning_efforts",
        ),
        chat_options=_declared_values(
            raw.get("chat_options", ()), CHAT_OPTIONS, f"{where}.chat_options"
        ),
        chat_fields=_field_dispositions(
            raw.get("chat_fields", {}), CHAT_FIELDS, f"{where}.chat_fields"
        ),
        completions_fields=_field_dispositions(
            raw.get("completions_fields", {}), COMPLETIONS_FIELDS, f"{where}.completions_fields"
        ),
        chat_repeats=_declared_values(
            raw.get("chat_repeats", ()), CHAT_REPEATS, f"{where}.chat_repeats"
        ),
        openai_contract=_contract_surfaces(raw.get("openai_contract"), f"{where}.openai_contract"),
        structured_output_streaming=_surface_capability(
            raw.get("structured_output_streaming"), f"{where}.structured_output_streaming"
        ),
        json_object=_surface_capability(raw.get("json_object"), f"{where}.json_object"),
        vision=_surface_capability(raw.get("vision"), f"{where}.vision"),
        image_url_fixture=_https_url(raw.get("image_url_fixture"), f"{where}.image_url_fixture"),
        cache_reuse=_cache_reuse(raw.get("cache_reuse"), f"{where}.cache_reuse"),
        effort_rendering=_effort_rendering(
            raw.get("effort_rendering"), f"{where}.effort_rendering"
        ),
        client_reuse=_bool(raw.get("client_reuse"), False, f"{where}.client_reuse"),
        client_sessions=_declared_values(
            raw.get("client_sessions", ()), CLIENT_SESSIONS, f"{where}.client_sessions"
        ),
        header_probe=_bool(raw.get("header_probe"), False, f"{where}.header_probe"),
        header_rewrites=_header_rewrites(
            raw.get("header_rewrites", {}), f"{where}.header_rewrites"
        ),
        scenario_checks=_declared_values(
            raw.get("scenario_checks", ()), SCENARIO_CHECKS, f"{where}.scenario_checks"
        ),
        client_model_names=_model_names(
            raw.get("client_model_names", ()), f"{where}.client_model_names"
        ),
        chat_quiet_efforts=_quiet_efforts(raw, where),
        forward_compat=evolution.forward_compat(
            raw.get("forward_compat"), f"{where}.forward_compat"
        ),
        byte_fidelity=evolution.byte_fidelity(raw.get("byte_fidelity"), f"{where}.byte_fidelity"),
        prompt_pins=evolution.prompt_pins(raw.get("prompt_pins"), f"{where}.prompt_pins"),
        contract_baseline=evolution.contract_baseline(
            raw.get("contract_baseline"), f"{where}.contract_baseline"
        ),
        render_pins=evolution.render_pins(raw.get("render_pins"), f"{where}.render_pins"),
        messages_features=_messages_features(
            raw.get("messages_features", {}), f"{where}.messages_features"
        ),
    )


def _quiet_efforts(raw: dict[str, Any], where: str) -> tuple[str, ...]:
    """Chat efforts that answer without reasoning, none of them a reasoning effort."""
    quiet = _declared_values(
        raw.get("chat_quiet_efforts", ()), CHAT_REASONING_EFFORTS, f"{where}.chat_quiet_efforts"
    )
    reasoning = raw.get("chat_reasoning_efforts", ())
    overlap = sorted(set(quiet) & set(reasoning if isinstance(reasoning, list) else ()))
    if overlap:
        raise ProfileError(
            f"{where}.chat_quiet_efforts: {overlap} also in chat_reasoning_efforts, "
            "whose case expects reasoning"
        )
    return quiet


def _model_names(value: Any, where: str) -> tuple[str, ...]:
    """Distinct model names, each non-empty and without whitespace."""
    names = _strings(value, where)
    if any(not name or any(c.isspace() for c in name) for name in names):
        raise ProfileError(f"{where}: expected model names without whitespace")
    if len(set(names)) != len(names):
        raise ProfileError(f"{where}: expected distinct model names")
    return names


def _responses_features(raw: Any, where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: expected a mapping of feature to disposition, got {raw!r}")
    for name, value in raw.items():
        if name not in RESPONSES_FEATURES:
            raise ProfileError(
                f"{where}.{name}: unknown feature; known: {', '.join(RESPONSES_FEATURES)}"
            )
        allowed = FEATURE_DISPOSITIONS
        if name in RESPONSES_REFERENCE_FEATURES:
            allowed = ("ignored", "rejected")  # a fresh reference resolves to nothing
        if value not in allowed:
            raise ProfileError(
                f"{where}.{name}: expected one of {', '.join(allowed)}, got {value!r}"
            )
    return dict(raw)


def _stateless_responses(raw: dict[str, Any], where: str) -> bool:
    stateless = _bool(raw.get("stateless_responses"), False, f"{where}.stateless_responses")
    for name in ("stateful_responses", "background_responses"):
        if stateless and _bool(raw.get(name), False, f"{where}.{name}"):
            raise ProfileError(f"{where}.stateless_responses: cannot be declared with {name}")
    return stateless


def _headers(raw: dict[str, Any]) -> dict[str, dict[str, Disposition]]:
    out: dict[str, dict[str, Disposition]] = {}
    for header, per_ingress in raw.items():
        if not isinstance(per_ingress, dict):
            raise ProfileError(f"headers.{header}: must be a mapping of ingress id -> disposition")
        out[str(header).lower()] = {
            str(ingress_id): _enum(
                Disposition, value, Disposition.IGNORED, f"headers.{header}.{ingress_id}"
            )
            for ingress_id, value in per_ingress.items()
        }
    return out


def _client(raw: dict[str, Any], base_dir: Path) -> Client:
    return Client(
        id=str(raw.get("id") or ""),
        surfaces=tuple(str(item) for item in (raw.get("surfaces") or [])),
        needs=tuple(str(item) for item in (raw.get("needs") or [])),
        binaries=_binaries(raw.get("binaries", []), f"clients({raw.get('id')}).binaries", base_dir),
    )


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _opt_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _opt_int(value: Any) -> int | None:
    return int(value) if value not in (None, "") else None


def _opt_positive_int(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ProfileError(f"{where}: expected a positive integer, got {value!r}")
    return value


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _effort_rendering(raw: Any, where: str) -> EffortRendering | None:
    """Parse the rendered-effort declaration, reporting every invalid value in one error."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: expected a mapping or null")
    _check_override_keys(raw, EffortRendering, where)
    problems: list[str] = []
    pattern = raw.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        problems.append(f"{where}.pattern: expected a regular expression")
    else:
        try:
            groups = re.compile(pattern).groups
        except re.error as error:
            problems.append(f"{where}.pattern: not a regular expression: {error}")
        else:
            if groups != 1:
                problems.append(
                    f"{where}.pattern: expected exactly one capture group, got {groups}"
                )
    known = (UNSET_EFFORT, *EFFORT_VALUES["chat_completions"])
    efforts = raw.get("efforts")
    markers: dict[str, str | None] = {}
    if not isinstance(efforts, dict) or not efforts:
        problems.append(f"{where}.efforts: expected a mapping of effort to the rendered marker")
    else:
        for effort, marker in efforts.items():
            if effort not in known:
                problems.append(
                    f"{where}.efforts.{effort}: unknown effort; known: {', '.join(known)}"
                )
            elif marker is None or (isinstance(marker, str) and marker):
                markers[effort] = marker
            elif type(marker) is int:
                markers[effort] = str(marker)
            else:
                problems.append(
                    f"{where}.efforts.{effort}: expected the marker text, or null for no marker"
                )
    selected = raw.get("surfaces", ["chat_completions"])
    if (
        not isinstance(selected, list)
        or not selected
        or any(item not in EFFORT_VALUES for item in selected)
        or len(set(selected)) != len(selected)
    ):
        problems.append(f"{where}.surfaces: select distinct names from {', '.join(EFFORT_VALUES)}")
    if problems:
        raise ProfileError("\n".join(problems))
    return EffortRendering(pattern=pattern, efforts=markers, surfaces=tuple(selected))


def _cache_reuse(raw: Any, where: str) -> CacheReuse | None:
    """Parse the cache probes, reporting every invalid value in one error."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: expected a mapping or null")
    _check_override_keys(
        raw,
        CacheReuse,
        where,
        nullable=(
            "replica_header",
            "affinity",
            "block_size",
            "cold_max_ratio",
            "stripped_system_prefix",
        ),
    )
    from ..http.redact import SECRET_HEADERS

    problems: list[str] = []
    ratio = raw.get("min_ratio")
    if not _finite(ratio) or not 0 < ratio <= 1:
        problems.append(f"{where}.min_ratio: expected a number greater than zero and at most one")
    words = raw.get("prefix_words", 1024)
    if type(words) is not int or not 128 <= words <= 4096:
        problems.append(f"{where}.prefix_words: expected an integer from 128 to 4096")
    single = raw.get("single_replica", False)
    if not isinstance(single, bool):
        problems.append(f"{where}.single_replica: expected true or false, got {single!r}")
    header = raw.get("replica_header") or None
    if header is not None and (
        not isinstance(header, str)
        or header.lower() in SECRET_HEADERS
        or any(c.isspace() or c == ":" for c in header)
    ):
        problems.append(f"{where}.replica_header: expected a non-credential header name")
    affinity = raw.get("affinity")
    if affinity is not None and affinity not in CACHE_AFFINITIES:
        problems.append(
            f"{where}.affinity: expected one of {list(CACHE_AFFINITIES)}, got {affinity!r}"
        )
    declared = [
        name
        for name, present in (
            ("single_replica", single is True),
            ("replica_header", header is not None),
            ("affinity", affinity is not None),
        )
        if present
    ]
    if len(declared) != 1:
        problems.append(
            f"{where}: declare exactly one of single_replica, replica_header or affinity"
            + (f"; got {', '.join(declared)}" if declared else "")
        )
    block = raw.get("block_size")
    if block is not None and (type(block) is not int or block <= 0):
        problems.append(f"{where}.block_size: expected a positive integer, got {block!r}")
    elif block is not None and type(words) is int and block > words:
        problems.append(
            f"{where}.block_size: {block} is more than prefix_words {words}; "
            "the probe prefix must fill at least one block"
        )
    withheld = raw.get("withheld_blocks", 0)
    if type(withheld) is not int or withheld < 0:
        problems.append(
            f"{where}.withheld_blocks: expected an integer of zero or more, got {withheld!r}"
        )
    elif withheld and block is None:
        problems.append(
            f"{where}.withheld_blocks: lowers block floors only; declare block_size with it"
        )
    elif (
        withheld
        and type(block) is int
        and block > 0
        and type(words) is int
        and block * (withheld + 1) > words
    ):
        problems.append(
            f"{where}.withheld_blocks: {withheld} withheld {block}-token blocks leave nothing of "
            f"a {words}-word probe prefix to read; it must fill more blocks than are withheld"
        )
    canonical = raw.get("canonical_tool_keys", False)
    if not isinstance(canonical, bool):
        problems.append(f"{where}.canonical_tool_keys: expected true or false, got {canonical!r}")
    selected = raw.get("surfaces", ["chat_completions"])
    if (
        not isinstance(selected, list)
        or not selected
        or any(item not in ("chat_completions", "messages", "responses") for item in selected)
    ):
        problems.append(f"{where}.surfaces: select Chat, Messages or Responses surface names")
    cold = raw.get("cold_max_ratio")
    if cold is not None and (not _finite(cold) or not 0 <= cold < (ratio if _finite(ratio) else 1)):
        problems.append(f"{where}.cold_max_ratio: expected zero or more, less than min_ratio")
    equivalent = raw.get("render_equivalent", [])
    if (
        not isinstance(equivalent, list)
        or len(equivalent) == 1
        or len(set(map(str, equivalent))) != len(equivalent)
        or any(item not in (selected if isinstance(selected, list) else []) for item in equivalent)
    ):
        problems.append(
            f"{where}.render_equivalent: expected two or more distinct names from surfaces"
        )
    stripped = raw.get("stripped_system_prefix")
    if stripped is not None and (not isinstance(stripped, str) or not stripped.strip()):
        problems.append(f"{where}.stripped_system_prefix: expected a non-empty string")
    shared = raw.get("shared_with", [])
    if (
        not isinstance(shared, list)
        or any(not isinstance(item, str) or not item for item in shared)
        or len(set(shared)) != len(shared)
    ):
        problems.append(f"{where}.shared_with: expected distinct ingress ids")
    settings = raw.get("equivalent_settings", [])
    if not isinstance(settings, list) or any(
        not isinstance(pair, list)
        or len(pair) != 2
        or not all(_setting_entry(entry, selected) for entry in pair)
        or parse_setting(pair[0]) == parse_setting(pair[1])
        for pair in settings
    ):
        problems.append(
            f"{where}.equivalent_settings: expected pairs of two different surface:setting "
            "entries, each naming one of surfaces and a value of its setting; Messages "
            f"`enabled` may add a thinking budget of at least {MESSAGES_MIN_THINKING_BUDGET}, "
            "as messages:enabled:2048"
        )
    if problems:
        raise ProfileError("\n".join(problems))
    return CacheReuse(
        min_ratio=float(ratio),
        surfaces=tuple(selected),
        prefix_words=words,
        single_replica=single,
        replica_header=header.lower() if header else None,
        affinity=affinity,
        block_size=block,
        withheld_blocks=withheld,
        canonical_tool_keys=canonical,
        cold_max_ratio=float(cold) if cold is not None else None,
        render_equivalent=tuple(equivalent),
        stripped_system_prefix=stripped,
        equivalent_settings=tuple(tuple(pair) for pair in settings),
        shared_with=tuple(shared),
    )


def _setting_entry(entry: Any, selected: Any) -> bool:
    """Whether `surface:setting` names a declared cache surface and a value of its setting."""
    if not isinstance(entry, str) or not isinstance(selected, list):
        return False
    parsed = parse_setting(entry)
    if parsed is None:
        return False
    name, value, _ = parsed
    return name in selected and value in SETTING_VALUES.get(name, ())


def _binaries(raw: Any, where: str, base_dir: Path) -> tuple[ClientBinary, ...]:
    import re

    if not isinstance(raw, list):
        raise ProfileError(f"{where}: expected a list of pinned Claude Code binaries")
    binaries = []
    for index, item in enumerate(raw):
        path = f"{where}[{index}]"
        if not isinstance(item, dict) or set(item) != {"version", "executable"}:
            raise ProfileError(f"{path}: expected version and executable")
        version = _string(item["version"], f"{path}.version")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ProfileError(f"{path}.version: expected an exact numeric version")
        if tuple(map(int, version.split("."))) < CLAUDE_CODE_MIN_VERSION:
            raise ProfileError(
                f"{path}.version: Claude Code harness requires version 2.1.0 or newer"
            )
        executable = _string(item["executable"], f"{path}.executable")
        if not executable:
            raise ProfileError(f"{path}.executable: cannot be empty")
        executable = Path(executable).expanduser()
        if not executable.is_absolute():
            executable = base_dir / executable
        binaries.append(ClientBinary(version, executable))
    if len({binary.version for binary in binaries}) != len(binaries):
        raise ProfileError(f"{where}: versions must be distinct")
    return tuple(binaries)


def _header_rewrites(raw: Any, where: str) -> dict[str, str]:
    from ..http.redact import SECRET_HEADERS

    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: expected a header/value mapping")
    result = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or name.lower() in SECRET_HEADERS:
            raise ProfileError(f"{where}: expected non-credential header names")
        result[name.lower()] = _string(value, f"{where}.{name}")
    return result


def _model_info(raw: Any, where: str) -> dict[str, Any]:
    """Expected `/v1/model/info` values: a boolean, number, string or list of strings each."""
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: expected a mapping of field to expected value, got {raw!r}")
    result: dict[str, Any] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise ProfileError(f"{where}: expected non-empty field names, got {name!r}")
        if isinstance(value, list):
            result[name] = _strings(value, f"{where}.{name}")
        elif isinstance(value, (bool, int, float, str)):
            result[name] = value
        else:
            raise ProfileError(
                f"{where}.{name}: expected a boolean, number, string or list of strings, "
                f"got {value!r}"
            )
    return result


def _messages_features(raw: Any, where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: expected a mapping of feature -> accepted or rejected")
    for name, outcome in raw.items():
        if name not in MESSAGES_FEATURES:
            raise ProfileError(
                f"{where}: unknown feature {name!r}; known: {', '.join(MESSAGES_FEATURES)}"
            )
        if outcome not in MESSAGES_FEATURE_OUTCOMES:
            raise ProfileError(f"{where}.{name}: expected accepted or rejected, got {outcome!r}")
    return {str(name): str(outcome) for name, outcome in raw.items()}
