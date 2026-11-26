"""YAML -> Profile, with diagnostics that name the path that is wrong.

Problems are found in two passes, and each reports every problem it finds rather than the first,
because a profile is edited as a whole and a check that reports one error per run wastes a round
trip each time. `load` checks types and shapes: a value that cannot be turned into an object at
all, such as a string where a list belongs. It raises one `ProfileError` with a line per problem,
each naming the file and the path. `validate` runs on a loaded profile and returns ranges,
cross-field and cross-section problems as `Diagnostic`s. A value an ingress inherits is checked
once, where it was declared.

Before any of that, each file of an `extends` chain is checked on its own for what composing
would hide: a top-level key that is not one, and an ingress id that is missing, is not a plain
name, or is named twice. Those are reported against the file that holds them.

`validate` is offline by design. It resolves no secrets and contacts no cluster, so a profile
can be written and checked on a laptop with no access to the fleet it describes. Missing
credentials and unreachable endpoints are preflight's job, and keeping them there is what
makes the distinction between "your profile is wrong" (exit 2) and "your vantage point is
wrong" (exit 3) meaningful.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import fields, replace
from functools import partial
from pathlib import Path
from typing import Any

import yaml

from ..http.redact import SECRET_HEADERS
from ..ingress.signals import Reveals, row_for, undeclarable
from ..surfaces import base as surfaces
from . import compose, evolution, parse, test_sets
from . import transport as transport_contract
from .errors import Diagnostic, ProfileError
from .fleet import (
    fleet_diagnostics,
    parse_allow,
    parse_fleet,
    parse_metrics_source,
    parse_signatures,
)
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
    SampleRule,
    Setup,
    Timeouts,
    Tls,
    Tools,
    Vantage,
    matches_case,
    parse_setting,
)

SUPPORTED_SCHEMA = "llmdv.profile/1"

# Every key a profile file may hold at its top level: the fields of `Profile` that a file sets,
# and `extends`. REFERENCE.md documents the same fields (tests/test_profile_reference.py).
TOP_LEVEL_KEYS = (
    "extends",
    *(item.name for item in fields(Profile) if item.name != "source_path"),
)

# An ingress id keys the merge of `extends` and becomes a pytest id, a report column, a
# diagnostic path and a file name.
_INGRESS_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def load(path: str | Path) -> Profile:
    """Parse a profile file, and the files it extends, into a Profile.

    Raises ProfileError on unusable input, naming for each problem the file that set the value.
    """
    return load_with_sources(path)[0]


def load_with_sources(path: str | Path) -> tuple[Profile, list[compose.Layer]]:
    """Load once, retaining the checked declarations for authoring tools."""
    chain = _layers(path)
    try:
        # A file that extends nothing resolves a relative client executable against its folder;
        # a chain's are already absolute.
        profile = _build(compose.resolved(chain), chain[-1].path, chain[-1].path.parent)
        return profile, chain
    except ProfileError as exc:
        raise ProfileError(
            *(
                f"{compose.origin(chain, problem.split(': ', 1)[0])}: {problem}"
                for problem in exc.problems
            )
        ) from exc


def composed_text(path: str | Path) -> str | None:
    """A profile that extends another as one self-contained file, with where each key was set.

    None for a profile that extends nothing, which is its own complete record, or that cannot be
    composed, which the loader reports.
    """
    try:
        chain = _layers(path)
    except ProfileError:
        return None
    return compose.render(chain) if len(chain) > 1 else None


def archived_text(path: str | Path) -> str | None:
    """What a run archives for `path` when a plain copy would not replay it.

    `composed_text`, unless a variable supplied a model id: then the whole profile, with each id
    this run tested written in. None when a copy of the file is the record.
    """
    try:
        profile, chain = load_with_sources(path)
    except ProfileError:
        return composed_text(path)
    pinned = pinned_model_ids(compose.resolved(chain, standalone=True), profile)
    if pinned is None:
        return composed_text(path)
    return (
        "# Archived by llmdv: each model id a public_id_env variable supplied is written in, so a\n"
        "# replay tests the names this run tested whatever its shell exports.\n"
        + yaml.safe_dump(pinned, sort_keys=False)
    )


def pinned_model_ids(document: dict[str, Any], profile: Profile) -> dict[str, Any] | None:
    """`document` with the model ids `profile` resolved written in, or None if no variable set one.

    `profile` is what `document` loaded to. Every ingress gets its id and stops reading a variable,
    not only those that read one: an ingress that opted out of the profile's variable would
    otherwise inherit the id written in at the top.
    """
    models = (profile.model, *(ingress.model for ingress in profile.ingresses))
    if not any(m.public_id_env and os.environ.get(m.public_id_env, "").strip() for m in models):
        return None
    top = {k: v for k, v in (document.get("model") or {}).items() if k != "public_id_env"}
    ids = {ingress.id: ingress.model.public_id for ingress in profile.ingresses}
    return {
        **document,
        "model": {**top, "public_id": profile.model.public_id},
        "ingresses": [
            {
                **entry,
                "model": {
                    **(entry.get("model") or {}),
                    "public_id": ids[entry["id"]],
                    "public_id_env": None,
                },
            }
            for entry in document["ingresses"]
        ],
    }


def _layers(path: str | Path) -> list[compose.Layer]:
    """The chain `path` names, each file checked for what composing would hide."""
    chain = compose.layers(path)
    problems = [problem for layer in chain for problem in _file_problems(layer)]
    inherited = False
    for layer in chain:
        if inherited and "ingresses" in layer.raw and layer.raw["ingresses"] is None:
            problems.append(
                f"{layer.path}: ingresses: null would remove every inherited ingress, and a "
                "child cannot remove one; select columns with -k instead"
            )
        inherited = inherited or isinstance(layer.raw.get("ingresses"), list)
    if problems:
        raise ProfileError(*problems)
    return chain


def _file_problems(layer: compose.Layer) -> list[str]:
    problems = []
    for key in layer.raw:
        if key in {"selection", "selections"}:
            replacement = "test_sets.default" if key == "selection" else "test_sets"
            problems.append(f"{layer.path}: {key}: removed; use {replacement}")
            continue
        if key not in TOP_LEVEL_KEYS:
            close = difflib.get_close_matches(str(key), TOP_LEVEL_KEYS, n=1)
            hint = f"did you mean {close[0]}?" if close else f"known: {', '.join(TOP_LEVEL_KEYS)}"
            shown = key if isinstance(key, str) and key.isprintable() else repr(key)
            problems.append(f"{layer.path}: {shown}: unknown top-level key; {hint}")
    entries = layer.raw.get("ingresses")
    seen: set[str] = set()
    for index, entry in enumerate(entries if isinstance(entries, list) else ()):
        if not isinstance(entry, dict):
            continue  # reported when the profile is built
        at = f"{layer.path}: ingresses[{index}]"
        ingress_id = entry.get("id")
        if "id" not in entry:
            problems.append(f"{at}.id: required")
        elif not isinstance(ingress_id, str) or not _INGRESS_ID.fullmatch(ingress_id):
            problems.append(
                f"{at}.id: expected a name of letters, digits, '.', '_' and '-' that starts "
                f"with a letter or a digit, got {ingress_id!r}"
            )
        elif ingress_id in seen:
            problems.append(f"{at}({ingress_id}): duplicate ingress id {ingress_id!r}")
        else:
            seen.add(ingress_id)
    return problems


def _attributed(profile: Profile, found: list[Diagnostic]) -> list[Diagnostic]:
    """Each finding with the file that set its value, for a profile that extends another."""
    try:
        chain = _layers(profile.source_path) if profile.source_path else []
    except ProfileError:
        return found
    if len(chain) < 2:
        return found
    return [replace(item, file=compose.origin(chain, item.path)) for item in found]


def validate(profile: Profile) -> list[Diagnostic]:
    """Offline checks across the whole profile. Errors block a run; warnings do not.

    For a profile that extends another, each finding names the file that set the value.
    """
    return _attributed(profile, _diagnostics(profile))


def _diagnostics(profile: Profile) -> list[Diagnostic]:
    out = _models(profile)

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

    # `load` refuses a declared layer graph that is not one; a profile built in code reaches
    # here unchecked, and a bad graph blocks its run before any request.
    try:
        profile.topology()
    except ProfileError as exc:
        for problem in exc.problems:
            path, _, message = problem.partition(": ")
            out.append(Diagnostic("error", path, message))

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

    out += fleet_diagnostics(profile)
    return out


def validate_models(profile: Profile) -> list[Diagnostic]:
    """Model rules shared by offline validation and native pytest before any requests.

    A problem is reported where it was declared: at `model` for a value every ingress inherits,
    and at an ingress only for a value that ingress overrides.
    """
    return _attributed(profile, _models(profile))


def _models(profile: Profile) -> list[Diagnostic]:
    out = _model_diagnostics("model", profile.model, None)
    for index, ingress in enumerate(profile.ingresses):
        where = f"ingresses[{index}]({ingress.id}).model"
        out += _model_diagnostics(where, ingress.model, profile.model)
    return out


def _model_diagnostics(where: str, model: Model, inherited: Model | None) -> list[Diagnostic]:
    def declared_here(name: str) -> bool:
        return inherited is None or getattr(model, name) != getattr(inherited, name)

    out = []
    if not model.public_id and declared_here("public_id"):
        out.append(Diagnostic("error", f"{where}.public_id", "required"))
    if (
        model.max_model_len is not None
        and model.max_model_len < 0
        and declared_here("max_model_len")
    ):
        out.append(Diagnostic("error", f"{where}.max_model_len", "must not be negative"))
    if (
        model.reasoning.is_separate_field
        and model.reasoning.is_inline
        and declared_here("reasoning")
    ):
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


def _build(raw: dict[str, Any], path: Path, clients_dir: Path) -> Profile:
    # What each ingress inherits. A top-level block that fails to parse is reported once, where
    # it was declared, and its ingresses then check only their own overrides.
    defaults: dict[str, dict[str, Any]] = {"model": {}, "capabilities": {}}

    def inherited(key: str, parse_block, usable=None):
        def run():
            block = _block(raw.get(key), key)
            try:
                value = parse_block(block)
            except ProfileError:
                # What still parses on its own is inherited, so a typo beside it does not make
                # every ingress's partial override fail for want of what it refines.
                if usable is not None:
                    defaults[key] = usable(block)
                raise
            defaults[key] = block
            return value

        return run

    try:
        profile = Profile(
            **_each(
                schema=lambda: str(raw.get("schema", SUPPORTED_SCHEMA)),
                name=lambda: str(raw.get("name") or path.stem),
                guide=lambda: _opt_str(raw.get("guide")),
                engine=lambda: _opt_str(raw.get("engine")),
                gateway=lambda: _opt_str(raw.get("gateway")),
                vantage=lambda: _enum(Vantage, raw.get("vantage"), Vantage.EXTERNAL, "vantage"),
                environment=lambda: _environment(_block(raw.get("environment"), "environment")),
                model=inherited("model", _model),
                capabilities=inherited("capabilities", _capabilities, _usable_capabilities),
                ingresses=lambda: _every(
                    partial(
                        _ingress,
                        item,
                        f"ingresses[{index}]",
                        defaults["model"],
                        defaults["capabilities"],
                    )
                    for index, item in _entries(raw.get("ingresses"), "ingresses")
                ),
                headers=lambda: _headers(_block(raw.get("headers"), "headers")),
                clients=lambda: _every(
                    partial(_client, item, f"clients[{index}]", clients_dir)
                    for index, item in _entries(raw.get("clients"), "clients")
                ),
                fleet_policy=lambda: _enum(
                    FleetPolicy, raw.get("fleet_policy"), FleetPolicy.DEDICATED, "fleet_policy"
                ),
                fleet=lambda: parse_fleet(raw.get("fleet")),
                known_failures=lambda: _known_failures(raw.get("known_failures")),
                test_sets=lambda: _test_sets(raw.get("test_sets")),
            ),
            source_path=path,
        )
    except ProfileError:
        raise
    except (TypeError, ValueError, AttributeError) as exc:
        # A shape no parser checks for. The file is named by `load`, which adds it to every line.
        raise ProfileError(str(exc)) from exc
    # A declared layer graph that is not one makes the profile unusable, so it is refused here
    # and a loaded profile's `topology()` and `chain()` never raise.
    profile.topology()
    return profile


def _each(**parsers: Callable[[], Any]) -> dict[str, Any]:
    """Run every parser and return their values, or raise once with every problem they found.

    A profile is edited as a whole, so one load reports all of its type and shape errors rather
    than one per run.
    """
    values: dict[str, Any] = {}
    problems: list[str] = []
    for name, parse_value in parsers.items():
        try:
            values[name] = parse_value()
        except ProfileError as exc:
            problems += exc.problems
    if problems:
        raise ProfileError(*problems)
    return values


def _every(parsers: Iterable[Callable[[], Any]]) -> tuple[Any, ...]:
    """`_each` over a list's entries, in order."""
    return tuple(_each(**{str(index): item for index, item in enumerate(parsers)}).values())


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
        check = parse.string(item.get("check", ""), f"{where}.check")
        if not re.search(r"[^*?]", check):
            # `*` alone would label every failure in the run, which is the one thing this
            # block must never be able to do.
            raise ProfileError(
                f"{where}.check: the first failing check is required, with at least one literal "
                f"character; got {check!r}"
            )
        issue = parse.string(item.get("issue", ""), f"{where}.issue")
        if not _ISSUE.match(issue):
            raise ProfileError(
                f"{where}.issue: expected 'owner/repo#N', '#N' or a URL, got {issue!r}"
            )
        until_raw = item.get("until") or {}
        if not isinstance(until_raw, dict):
            raise ProfileError(f"{where}.until: expected a mapping of signal to value")
        until = {}
        for name, value in until_raw.items():
            signal = parse.string(name, f"{where}.until").lower()
            if not isinstance(value, str) or not signal or not value:
                # YAML reads `1.100` as the float 1.1 and `1.97` as 1.97: a version must be
                # quoted, or the comparison is made against a number that is not the version.
                raise ProfileError(
                    f"{where}.until.{name}: expected a quoted version string, got {value!r}"
                )
            until[signal] = value
        cases = parse.strings(item.get("cases") or [], f"{where}.cases")
        _refuse_renamed_cases(cases, f"{where}.cases")
        out.append(
            KnownFailure(
                check=check,
                issue=issue,
                cases=cases,
                ingresses=_one_or_more(item.get("ingress"), f"{where}.ingress"),
                fix=parse.string(item["fix"], f"{where}.fix")
                if item.get("fix") is not None
                else None,
                until=until,
            )
        )
    return tuple(out)


# Case ids that were renamed, so an entry naming the old one would silently stop matching.
_RENAMED_CASES = {"field_options.vllm_observable": "field_options.extension_observable"}


def _names_case(pattern: str, case_id: str) -> bool:
    return matches_case((pattern,), case_id, None) or pattern.startswith(f"{case_id}[")


def _refuse_renamed_cases(cases: tuple[str, ...], where: str) -> None:
    for index, pattern in enumerate(cases):
        for old, new in _RENAMED_CASES.items():
            if _names_case(pattern, old) and not _names_case(pattern, new):
                raise ProfileError(
                    f"{where}[{index}]: {pattern!r} names {old!r}, which was renamed; use {new!r}"
                )


def _case_patterns(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    globs = parse.strings(raw, where)
    if not globs:
        raise ProfileError(
            f"{where}: an empty list selects no case; leave the key out, or set it to null to "
            "clear an inherited one"
        )
    for index, glob in enumerate(globs):
        parse.string(glob, f"{where}[{index}]", non_empty=True)
    _refuse_renamed_cases(globs, where)
    return globs


def _test_sets(raw: Any) -> dict[str, tuple[str, ...]]:
    found = {}
    for name, cases in _block(raw, "test_sets").items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise ProfileError(
                "test_sets: names must start with a lowercase letter and contain "
                "only lowercase letters, digits, hyphens or underscores"
            )
        if name in test_sets.BUILTIN:
            raise ProfileError(f"test_sets.{name}: reserved built-in name; choose another name")
        if cases is not None:  # null removes an inherited test set
            found[name] = _case_patterns(cases, f"test_sets.{name}")
    return found


def _block(value: Any, where: str) -> dict[str, Any]:
    """An optional mapping: empty when undeclared."""
    return {} if value is None else parse.mapping(value, where)


def _entries(value: Any, where: str) -> list[tuple[int, dict[str, Any]]]:
    """An optional list of mappings, each with its index, so a problem names the entry."""
    if value is None:
        return []
    return [
        (index, parse.mapping(item, f"{where}[{index}]"))
        for index, item in enumerate(parse.items(value, where))
    ]


def _number(convert, value: Any, where: str):
    """A number as `convert` reads it, naming the path when it cannot."""
    try:
        return convert(value)
    except (TypeError, ValueError):
        raise ProfileError(f"{where}: expected a number, got {value!r}") from None


def _one_or_more(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return parse.strings(value, where)


# Top-level keys that never change what a run asserts, so they never move the digest.
_UNDIGESTED = frozenset({"known_failures", "test_sets"})


def contract_digest(path: str | Path) -> str | None:
    """A short digest of the declarations in a profile, or None when it cannot be read.

    Hashes the parsed document rather than its bytes, with `known_failures` and
    `test_sets` left out: comments carry probes, the known-failure list carries triage, and
    a test set changes which cases run, never what a case asserts. A digest that moved
    with them would drop every earlier run from a comparison window (`compare/diff.py`)
    each time a finding
    was filed, a comment updated or a lane narrowed; a comparison reads which cases each run
    selected from its `outcome.json` instead. Where a
    client binary sits on this machine is left out for the same reason: its `version` is the
    declaration, and its path differs between two checkouts of one profile.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if isinstance(raw, dict) and "extends" in raw:
        # The declarations a run is judged against are the composed ones.
        try:
            raw = compose.resolved(compose.layers(path))
        except ProfileError:
            return None
    if isinstance(raw, dict):
        raw = {name: value for name, value in raw.items() if name not in _UNDIGESTED}
        if isinstance(raw.get("clients"), list):
            raw["clients"] = [_without_executables(client) for client in raw["clients"]]
    canonical = json.dumps(_stringify_keys(raw), sort_keys=True, default=str, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def _without_executables(client: Any) -> Any:
    """`client` with each binary's path dropped; any other shape is returned as it is."""
    if not isinstance(client, dict) or not isinstance(client.get("binaries"), list):
        return client
    binaries = [
        {key: value for key, value in binary.items() if key != "executable"}
        if isinstance(binary, dict)
        else binary
        for binary in client["binaries"]
    ]
    return {**client, "binaries": binaries}


def _stringify_keys(value: Any) -> Any:
    """Mapping keys as strings throughout, so `sort_keys` never compares an int with a str."""
    if isinstance(value, dict):
        return {str(k): _stringify_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stringify_keys(v) for v in value]
    return value


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
        metrics=parse_metrics_source(raw.get("metrics")),
    )


def _check_override_keys(raw, cls, where, nullable=(), renames=None) -> None:
    """Overrides are partial mappings, but misspellings and invalid clears are errors."""
    names = [(renames or {}).get(item.name, item.name) for item in fields(cls)]
    parse.fields(raw, names, where, nullable=nullable)


def _optional_strings(value: Any, where: str) -> tuple[str, ...]:
    return () if value is None else parse.strings(value, where)


def _markup(value: Any, where: str) -> tuple[str, ...]:
    markup = parse.strings(value, where)
    for index, item in enumerate(markup):
        if not item:
            raise ProfileError(f"{where}[{index}]: an empty string matches every answer")
    return markup


def _https_url(value: Any, where: str) -> str | None:
    """An absolute https URL with a host, or None when undeclared."""
    if value is None:
        return None
    url = parse.string(value, where)
    if not re.match(r"^https://[^/\s?#]+(/\S*)?$", url):
        raise ProfileError(f"{where}: expected an https:// URL with a host, got {value!r}")
    return url


def _nullable_string(value: Any, where: str) -> str | None:
    return None if value is None else parse.string(value, where) or None


def _ingress_model(override: Any, defaults: dict[str, Any], where: str) -> Model:
    _check_override_keys(
        override, Model, where, nullable=("max_model_len", "answer_max_tokens", "public_id_env")
    )
    _check_override_keys(
        override.get("reasoning", {}),
        Reasoning,
        f"{where}.reasoning",
        nullable=("parser", "field", "inline_tag"),
        renames={"field_name": "field"},
    )
    _check_override_keys(override.get("tools", {}), Tools, f"{where}.tools", nullable=("parser",))
    return _model(compose.merge(defaults, override), where)


# Capability keys that were renamed, and the name that replaced each. Refused rather than aliased,
# so a profile says what it means under one name.
_RENAMED_CAPABILITIES = {"vllm_options": "extension_options"}


def _renamed_capabilities(raw: Any, where: str) -> list[str]:
    """One problem per renamed key `raw` still declares, reported with the block's others."""
    if not isinstance(raw, dict):
        return []  # reported by the block's own parser
    return [
        f"{where}.{old}: renamed; declare {where}.{new} instead"
        for old, new in _RENAMED_CAPABILITIES.items()
        if old in raw
    ]


# Capability keys that no longer exist, and what says the same thing now.
_REMOVED_CAPABILITIES = {
    "responses": "whether an ingress serves the Responses API is its `surfaces` list",
}

# Capabilities an ingress override may clear with `null`, back to undeclared.
_NULLABLE_CAPABILITIES = (
    "cache_reuse",
    "effort_rendering",
    "model_info",
    "model_retrieval",
    "responses_input_tokens",
    "responses_compact",
)


def _capability_keys(
    raw: dict[str, Any], where: str, nullable: tuple[str, ...] | None
) -> tuple[dict[str, Any], list[str]]:
    """The block's keys llmdv knows, and one problem per key it does not, all at once.

    A renamed key is named as renamed, not again as unknown. With `nullable`, a null on any
    other key is a problem too. The known keys go on to the block's own checks, so a typo never
    hides a bad value beside it.
    """
    known = {item.name for item in fields(Capabilities)}
    problems = _renamed_capabilities(raw, where)
    kept: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _RENAMED_CAPABILITIES:
            continue
        if key in _REMOVED_CAPABILITIES:
            problems.append(f"{where}.{key}: removed; {_REMOVED_CAPABILITIES[key]}")
        elif key not in known:
            close = difflib.get_close_matches(str(key), sorted(known), n=1)
            hint = f"; did you mean {close[0]}?" if close else ""
            problems.append(f"{where}.{key}: unknown field{hint}")
        elif nullable is not None and value is None and key not in nullable:
            problems.append(f"{where}.{key}: cannot be null")
        else:
            kept[key] = value
    return kept, problems


def _ingress_capabilities(override: Any, defaults: dict[str, Any], where: str) -> Capabilities:
    if not isinstance(override, dict):
        _check_override_keys(override, Capabilities, where)  # names the expected mapping
    kept, problems = _capability_keys(override, where, _NULLABLE_CAPABILITIES)
    # The defaults were checked where they were declared.
    return _capability_block(compose.merge(defaults, kept), where, problems)


def _public_id(raw: dict[str, Any], where: str) -> str:
    """`public_id`, or the value of the variable `public_id_env` names when it is set."""
    default = parse.string(raw.get("public_id", ""), f"{where}.public_id")
    env = _nullable_string(raw.get("public_id_env"), f"{where}.public_id_env")
    return (os.environ.get(env, "").strip() if env else "") or default


def _model(raw: dict[str, Any], where: str = "model") -> Model:
    reasoning_raw = raw.get("reasoning") or {}
    tools_raw = raw.get("tools") or {}
    return Model(
        **_each(
            public_id=lambda: _public_id(raw, where),
            public_id_env=lambda: _nullable_string(
                raw.get("public_id_env"), f"{where}.public_id_env"
            ),
            aliases=lambda: parse.strings(raw.get("aliases", ()), f"{where}.aliases"),
            max_model_len=lambda: _max_model_len(raw.get("max_model_len"), where),
            answer_max_tokens=lambda: _opt_positive_int(
                raw.get("answer_max_tokens"), f"{where}.answer_max_tokens"
            ),
            reasoning=lambda: Reasoning(
                parser=_nullable_string(reasoning_raw.get("parser"), f"{where}.reasoning.parser"),
                field_name=_nullable_string(reasoning_raw.get("field"), f"{where}.reasoning.field"),
                inline_tag=_nullable_string(
                    reasoning_raw.get("inline_tag"),
                    f"{where}.reasoning.inline_tag",
                ),
                default_on=parse.flag(
                    reasoning_raw.get("default_on"),
                    False,
                    f"{where}.reasoning.default_on",
                ),
            ),
            tools=lambda: Tools(
                parser=_nullable_string(tools_raw.get("parser"), f"{where}.tools.parser"),
                auto_choice=parse.flag(
                    tools_raw.get("auto_choice"),
                    False,
                    f"{where}.tools.auto_choice",
                ),
                forced_finish_reason=parse.one_of(
                    tools_raw.get("forced_finish_reason"),
                    CHAT_FORCED_TOOL_FINISHES,
                    f"{where}.tools.forced_finish_reason",
                    "tool_calls",
                ),
                markup=_markup(tools_raw.get("markup", ()), f"{where}.tools.markup"),
                strict_level=parse.one_of(
                    tools_raw.get("strict_level"),
                    TOOL_STRICT_LEVELS,
                    f"{where}.tools.strict_level",
                    "auto",
                ),
            ),
        )
    )


def _max_model_len(limit: Any, where: str) -> int | None:
    if limit is not None and type(limit) is not int:
        raise ProfileError(f"{where}.max_model_len: expected an integer or null, got {limit!r}")
    return limit


def _base_url(raw: dict[str, Any], at: str) -> str:
    """`base_url`, or the value of the variable `base_url_env` names. Never both."""
    env = _opt_str(raw.get("base_url_env"))
    if env and raw.get("base_url"):
        raise ProfileError(f"{at}: declare base_url or base_url_env, not both")
    value = os.environ.get(env, "") if env else str(raw.get("base_url") or "")
    return value.strip().rstrip("/")


# The product names `kind` used to take. A profile that still names one is told the position
# that replaces it, rather than only the list of valid kinds.
_PRODUCT_KINDS = {"litellm": "gateway", "llmd_router": "router", "vllm": "engine"}

# Run identity names the products at the top level (`engine`, `gateway`), where a profile
# without that layer as an ingress can still state it.
_RETIRED_INGRESS_KEYS = {
    "role": "kind for the layer's position, and the top-level engine or gateway for its product",
    "implementation": "the top-level engine or gateway",
}


# An HTTP field name (RFC 9110 token): no spaces, no colon, not empty.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9a-z-]+")


def _version_headers(value: Any, at: str) -> tuple[str, ...]:
    """Headers an operator declares as naming the build, which the evidence table does not know.

    Never one any backend row matches, exactly or by prefix (a declared header is published in
    outcome.json); never a credential, a forwarded host or one whose value changes with every
    request (`signals.undeclarable`); never one the table already names exactly as a build,
    recorded value or backend (that would change its meaning for one profile); and never one
    twice. A header the table publishes without calling it a build, such as `server`, may be
    declared: it then names the build at that ingress only.
    """
    raw_names = _optional_strings(value, f"{at}.version_headers")
    names = tuple(name.strip().lower() for name in raw_names)
    problems = []
    for index, name in enumerate(names):
        where = f"{at}.version_headers[{index}]"
        reason = undeclarable(name)
        if not _HEADER_NAME.fullmatch(name):
            problems.append(f"{where}: {raw_names[index]!r} is not a header name")
        elif reason == "secret":
            problems.append(
                f"{where}: {name!r} carries a credential or a session, and a version header is "
                "published in outcome.json; it cannot be a version header"
            )
        elif reason == "per_request":
            problems.append(
                f"{where}: {name!r} changes from one request to the next (a request id, a quota "
                "or a time), so its values would read as a rollout on every run; it cannot be a "
                "version header"
            )
        elif reason == "host" or (
            (row := row_for(name)) is not None and row.reveals is Reveals.BACKEND
        ):
            problems.append(
                f"{where}: {name!r} may name a host or a Service, so it is recorded for "
                "attribution and never published; it cannot be a version header"
            )
        elif (
            (row := row_for(name)) is not None
            and row.header is not None
            and not (row.reveals is Reveals.VERSION and not row.build)
        ):
            problems.append(
                f"{where}: {name!r} is already in the evidence table (src/llmdv/ingress/"
                "signals.py), which says what it reveals; declare only a header it does not "
                "know, or one it publishes without calling a build, such as server"
            )
        elif name in names[:index]:
            problems.append(f"{where}: {name!r} is declared twice")
    if problems:
        raise ProfileError(*problems)
    return names


def _refuse_retired_keys(raw: dict[str, Any], at: str) -> None:
    problems = [
        f"{at}.{key}: no longer an ingress field; use {replacement}"
        for key, replacement in _RETIRED_INGRESS_KEYS.items()
        if key in raw
    ]
    if problems:
        raise ProfileError(*problems)


def _kind(value: Any, where: str) -> IngressKind:
    """Where the ingress sits. Required: a silent default would misplace a layer unnoticed."""
    valid = ", ".join(member.value for member in IngressKind)
    if value is None:
        raise ProfileError(f"{where}: required; one of {valid}")
    # Case and padding are forgiven for a position as for a product name: `Engine` is `engine`.
    name = value.strip().lower() if isinstance(value, str) else None
    if name in _PRODUCT_KINDS:
        raise ProfileError(
            f"{where}: {value!r} names a product, not a position; "
            f"use {_PRODUCT_KINDS[name]!r} (one of {valid})"
        )
    return _enum(IngressKind, name if name is not None else value, None, where)


def _ingress(
    raw: dict[str, Any],
    where: str,
    model_defaults: dict[str, Any],
    capability_defaults: dict[str, Any],
) -> Ingress:
    ingress_id = str(raw.get("id") or "")
    at = f"{where}({ingress_id})"
    fields = _each(
        retired=lambda: _refuse_retired_keys(raw, at),
        model=lambda: _ingress_model(raw.get("model", {}), model_defaults, f"{at}.model"),
        capabilities=lambda: _ingress_capabilities(
            raw.get("capabilities", {}), capability_defaults, f"{at}.capabilities"
        ),
        kind=lambda: _kind(raw.get("kind"), f"{at}.kind"),
        # Not `_optional_strings`: absent and `[]` mean different graphs.
        fronts=lambda: (
            None if raw.get("fronts") is None else parse.strings(raw["fronts"], f"{at}.fronts")
        ),
        base_url=lambda: _base_url(raw, at),
        base_url_env=lambda: _opt_str(raw.get("base_url_env")),
        auth=lambda: _auth(_block(raw.get("auth"), f"{at}.auth"), f"{at}.auth"),
        health_path=lambda: str(raw.get("health_path") or "/health"),
        version_headers=lambda: _version_headers(raw.get("version_headers"), at),
        surfaces=lambda: _optional_strings(raw.get("surfaces"), f"{at}.surfaces"),
        timeouts=lambda: _timeouts(_block(raw.get("timeouts"), f"{at}.timeouts"), at),
        error_status=lambda: _error_status(
            _block(raw.get("error_status"), f"{at}.error_status"), f"{at}.error_status"
        ),
        unstructured_errors=lambda: _optional_strings(
            raw.get("unstructured_errors"), f"{at}.unstructured_errors"
        ),
        rate_limited=lambda: parse.flag(raw.get("rate_limited"), False, f"{at}.rate_limited"),
        logs=lambda: _every(
            partial(_log_source, item, f"{at}.logs[{index}]")
            for index, item in _entries(raw.get("logs"), f"{at}.logs")
        ),
        tls=lambda: Tls(
            verify=parse.flag(
                _block(raw.get("tls"), f"{at}.tls").get("verify"), True, f"{at}.tls.verify"
            )
        ),
        transport=lambda: transport_contract.parse(raw.get("transport"), f"{at}.transport"),
        setup=lambda: _setup(_block(raw.get("setup"), f"{at}.setup"), f"{at}.setup"),
    )
    fields.pop("retired")
    ingress = Ingress(id=ingress_id, **fields)
    _check_http_redirect(ingress, at)
    return ingress


def _auth(raw: dict[str, Any], where: str) -> Auth:
    return Auth(
        type=_enum(AuthType, raw.get("type"), AuthType.NONE, f"{where}.type"),
        key_env=_opt_str(raw.get("key_env")),
    )


def _timeouts(raw: dict[str, Any], at: str) -> Timeouts:
    return Timeouts(
        **_each(
            **{
                name: partial(_number, float, raw.get(name, default), f"{at}.timeouts.{name}")
                for name, default in (("connect", 5.0), ("request", 600.0), ("stream_idle", 60.0))
            }
        )
    )


def _setup(raw: dict[str, Any], where: str) -> Setup:
    return Setup(
        **_each(
            proxy=lambda: _enum(ProxyMode, raw.get("proxy"), ProxyMode.BYPASS, f"{where}.proxy"),
            requires=lambda: _every(
                partial(_requirement, item, f"{where}.requires[{index}]")
                for index, item in _entries(raw.get("requires"), f"{where}.requires")
            ),
        )
    )


def _error_status(raw: dict[str, Any], where: str) -> dict[str, tuple[int, ...]]:
    """Statuses per error class. A scalar is a one-element list."""
    out = {}
    for key, value in raw.items():
        at = f"{where}.{key}"
        if isinstance(value, (list, tuple)):
            codes = [(f"{at}[{index}]", code) for index, code in enumerate(value)]
        else:
            codes = [(at, value)]
        out[str(key)] = tuple(_number(int, code, path) for path, code in codes)
    return out


def _check_http_redirect(ingress: Ingress, at: str) -> None:
    """`transport.http_redirect` belongs on an ingress reached over TLS on the default port.

    Only a literal `base_url` is checked here. A value from `base_url_env` is the operator's
    environment rather than the profile, so preflight checks it and names the variable.
    """
    if ingress.transport.http_redirect is None or ingress.base_url_env:
        return
    reason = transport_contract.plain_http_url(ingress.base_url)[1]
    if reason:
        raise ProfileError(f"{at}: {reason}")


def _requirement(raw: dict[str, Any], where: str) -> Requirement:
    return Requirement(
        kind=str(raw.get("kind") or "port_forward"),
        local_port=_opt_int(raw.get("local_port"), f"{where}.local_port"),
        remote_port=_opt_int(raw.get("remote_port"), f"{where}.remote_port"),
        namespace=_opt_str(raw.get("namespace")),
        target=_opt_str(raw.get("target")),
        selector=_opt_str(raw.get("selector")),
        command=_opt_str(raw.get("command")),
    )


def _log_source(raw: dict[str, Any], where: str) -> LogSource:
    return LogSource(
        selector=_opt_str(raw.get("selector")),
        target=_opt_str(raw.get("target")),
        container=_opt_str(raw.get("container")),
        tail=_number(int, raw.get("tail", 2000), f"{where}.tail"),
        forbid=parse_signatures(raw.get("forbid"), f"{where}.forbid"),
        allow=parse_allow(raw.get("allow"), f"{where}.allow"),
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
    values = parse.strings(value, where)
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


def _usable_capabilities(raw: dict[str, Any]) -> dict[str, Any]:
    """The known keys of a top-level block that failed, if they parse on their own, else none.

    Ingresses inherit these, so a typo at the top level is reported there once and does not
    turn into a false error on every ingress whose override refines an inherited value. A bad
    value is not inherited, so it is not reported again per ingress either.
    """
    kept, _ = _capability_keys(raw, "capabilities", None)
    try:
        _capability_block(kept, "capabilities", [])
    except ProfileError:
        return {}
    return kept


def _capabilities(raw: dict[str, Any], where: str = "capabilities") -> Capabilities:
    kept, problems = _capability_keys(raw, where, None) if isinstance(raw, dict) else (raw, [])
    return _capability_block(kept, where, problems)


def _capability_block(raw: dict[str, Any], where: str, earlier: list[str]) -> Capabilities:
    """The block's capabilities, or every problem in it, `earlier` first, raised once."""
    try:
        values = _each(
            prefix_stability=lambda: _surface_capability(
                raw.get("prefix_stability"), f"{where}.prefix_stability"
            ),
            count_tokens=lambda: parse.flag(
                raw.get("count_tokens"), False, f"{where}.count_tokens"
            ),
            anthropic_betas=lambda: parse.strings(
                raw.get("anthropic_betas", ()), f"{where}.anthropic_betas"
            ),
            messages_checks=lambda: _declared_values(
                raw.get("messages_checks", ()), MESSAGES_CHECKS, f"{where}.messages_checks"
            ),
            background_responses=lambda: parse.flag(
                raw.get("background_responses"), False, f"{where}.background_responses"
            ),
            stateful_responses=lambda: parse.flag(
                raw.get("stateful_responses"), False, f"{where}.stateful_responses"
            ),
            stateless_responses=lambda: _stateless_responses(raw, where),
            model_info=lambda: _model_info(raw.get("model_info") or {}, f"{where}.model_info"),
            model_retrieval=lambda: (
                None
                if raw.get("model_retrieval") is None
                else parse.flag(raw.get("model_retrieval"), False, f"{where}.model_retrieval")
            ),
            responses_compact=lambda: (
                None
                if raw.get("responses_compact") is None
                else parse.flag(raw.get("responses_compact"), False, f"{where}.responses_compact")
            ),
            responses_input_tokens=lambda: (
                None
                if raw.get("responses_input_tokens") is None
                else parse.flag(
                    raw.get("responses_input_tokens"), False, f"{where}.responses_input_tokens"
                )
            ),
            responses_features=lambda: _responses_features(
                raw.get("responses_features", {}), f"{where}.responses_features"
            ),
            structured_output=lambda: _surface_capability(
                raw.get("structured_output"), f"{where}.structured_output"
            ),
            reasoning_tokens=lambda: _surface_capability(
                raw.get("reasoning_tokens"), f"{where}.reasoning_tokens"
            ),
            extension_options=lambda: parse.strings(
                raw.get("extension_options", ()), f"{where}.extension_options"
            ),
            messages_thinking=lambda: _declared_values(
                raw.get("messages_thinking", ()),
                MESSAGES_THINKING_MODES,
                f"{where}.messages_thinking",
            ),
            messages_efforts=lambda: _declared_values(
                raw.get("messages_efforts", ()), MESSAGES_EFFORTS, f"{where}.messages_efforts"
            ),
            chat_reasoning_efforts=lambda: _declared_values(
                raw.get("chat_reasoning_efforts", ()),
                CHAT_REASONING_EFFORTS,
                f"{where}.chat_reasoning_efforts",
            ),
            responses_options=lambda: _declared_values(
                raw.get("responses_options", ()), RESPONSES_OPTIONS, f"{where}.responses_options"
            ),
            responses_reasoning_efforts=lambda: _declared_values(
                raw.get("responses_reasoning_efforts", ()),
                RESPONSES_REASONING_EFFORTS,
                f"{where}.responses_reasoning_efforts",
            ),
            chat_options=lambda: _declared_values(
                raw.get("chat_options", ()), CHAT_OPTIONS, f"{where}.chat_options"
            ),
            chat_fields=lambda: _field_dispositions(
                raw.get("chat_fields", {}), CHAT_FIELDS, f"{where}.chat_fields"
            ),
            completions_fields=lambda: _field_dispositions(
                raw.get("completions_fields", {}), COMPLETIONS_FIELDS, f"{where}.completions_fields"
            ),
            chat_repeats=lambda: _declared_values(
                raw.get("chat_repeats", ()), CHAT_REPEATS, f"{where}.chat_repeats"
            ),
            deterministic_repeats=lambda: parse.flag(
                raw.get("deterministic_repeats"), False, f"{where}.deterministic_repeats"
            ),
            samples=lambda: _samples(raw.get("samples"), f"{where}.samples"),
            openai_contract=lambda: _contract_surfaces(
                raw.get("openai_contract"), f"{where}.openai_contract"
            ),
            structured_output_streaming=lambda: _surface_capability(
                raw.get("structured_output_streaming"), f"{where}.structured_output_streaming"
            ),
            json_object=lambda: _surface_capability(raw.get("json_object"), f"{where}.json_object"),
            vision=lambda: _surface_capability(raw.get("vision"), f"{where}.vision"),
            image_url_fixture=lambda: _https_url(
                raw.get("image_url_fixture"), f"{where}.image_url_fixture"
            ),
            cache_reuse=lambda: _cache_reuse(raw.get("cache_reuse"), f"{where}.cache_reuse"),
            effort_rendering=lambda: _effort_rendering(
                raw.get("effort_rendering"), f"{where}.effort_rendering"
            ),
            client_reuse=lambda: parse.flag(
                raw.get("client_reuse"), False, f"{where}.client_reuse"
            ),
            client_sessions=lambda: _declared_values(
                raw.get("client_sessions", ()), CLIENT_SESSIONS, f"{where}.client_sessions"
            ),
            header_probe=lambda: parse.flag(
                raw.get("header_probe"), False, f"{where}.header_probe"
            ),
            header_rewrites=lambda: _header_rewrites(
                raw.get("header_rewrites", {}), f"{where}.header_rewrites"
            ),
            scenario_checks=lambda: _declared_values(
                raw.get("scenario_checks", ()), SCENARIO_CHECKS, f"{where}.scenario_checks"
            ),
            client_model_names=lambda: _model_names(
                raw.get("client_model_names", ()), f"{where}.client_model_names"
            ),
            chat_quiet_efforts=lambda: _quiet_efforts(raw, where),
            forward_compat=lambda: evolution.forward_compat(
                raw.get("forward_compat"), f"{where}.forward_compat"
            ),
            byte_fidelity=lambda: evolution.byte_fidelity(
                raw.get("byte_fidelity"), f"{where}.byte_fidelity"
            ),
            prompt_pins=lambda: evolution.prompt_pins(
                raw.get("prompt_pins"), f"{where}.prompt_pins"
            ),
            contract_baseline=lambda: evolution.contract_baseline(
                raw.get("contract_baseline"), f"{where}.contract_baseline"
            ),
            render_pins=lambda: evolution.render_pins(
                raw.get("render_pins"), f"{where}.render_pins"
            ),
            messages_features=lambda: _messages_features(
                raw.get("messages_features", {}), f"{where}.messages_features"
            ),
        )
    except ProfileError as exc:
        raise ProfileError(*earlier, *exc.problems) from None
    if earlier:
        raise ProfileError(*earlier)
    return Capabilities(**values)


def _samples(value: Any, where: str) -> dict[str, SampleRule]:
    """Case id -> sample count and bounds. Which bounds a case takes depends on its rule's
    kind, so that is checked where the case resolves its rule (assertions/samples.py)."""
    rules: dict[str, SampleRule] = {}
    for case_id, entry in _block(value, where).items():
        at = f"{where}.{case_id}"
        if not isinstance(case_id, str) or "." not in case_id:
            raise ProfileError(f"{at}: expected a case id such as openai_chat.greedy_repeat")
        entry = parse.mapping(entry, at)
        unknown = sorted(set(entry) - {"n", "min", "max", "slack"})
        if unknown:
            raise ProfileError(f"{at}: unknown keys {unknown}; known: n, min, max, slack")
        n = entry.get("n")
        if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 1):
            raise ProfileError(f"{at}.n: expected a positive integer, got {n!r}")
        for name in ("min", "max", "slack"):
            bound = entry.get(name)
            if bound is not None and (
                isinstance(bound, bool)
                or not isinstance(bound, int | float)
                or not math.isfinite(bound)
            ):
                raise ProfileError(f"{at}.{name}: expected a finite number, got {bound!r}")
        rules[case_id] = SampleRule(**entry)
    return rules


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
    names = parse.strings(value, where)
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
    stateless = parse.flag(raw.get("stateless_responses"), False, f"{where}.stateless_responses")
    for name in ("stateful_responses", "background_responses"):
        if stateless and parse.flag(raw.get(name), False, f"{where}.{name}"):
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


def _client(raw: dict[str, Any], where: str, base_dir: Path) -> Client:
    client_id = str(raw.get("id") or "")
    at = f"{where}({client_id})"
    return Client(
        id=client_id,
        surfaces=_optional_strings(raw.get("surfaces"), f"{at}.surfaces"),
        needs=_optional_strings(raw.get("needs"), f"{at}.needs"),
        binaries=_binaries(raw.get("binaries", []), f"{at}.binaries", base_dir),
    )


def _opt_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _opt_int(value: Any, where: str) -> int | None:
    return _number(int, value, where) if value not in (None, "") else None


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
        raise ProfileError(*problems)
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
        raise ProfileError(*problems)
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
    if not isinstance(raw, list):
        raise ProfileError(f"{where}: expected a list of pinned Claude Code binaries")
    binaries = []
    for index, item in enumerate(raw):
        path = f"{where}[{index}]"
        if not isinstance(item, dict) or set(item) != {"version", "executable"}:
            raise ProfileError(f"{path}: expected version and executable")
        version = parse.string(item["version"], f"{path}.version")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ProfileError(f"{path}.version: expected an exact numeric version")
        if tuple(map(int, version.split("."))) < CLAUDE_CODE_MIN_VERSION:
            raise ProfileError(
                f"{path}.version: Claude Code harness requires version 2.1.0 or newer"
            )
        executable = parse.string(item["executable"], f"{path}.executable")
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
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: expected a header/value mapping")
    result = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or name.lower() in SECRET_HEADERS:
            raise ProfileError(f"{where}: expected non-credential header names")
        result[name.lower()] = parse.string(value, f"{where}.{name}")
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
            result[name] = parse.strings(value, f"{where}.{name}")
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
