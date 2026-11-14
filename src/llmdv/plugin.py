"""The llmdv pytest plugin: fixtures, the ingress matrix, skip classification, reporting.

Registered through a `pytest11` entry point, so `pytest --llmdv-profile=... -m contract` behaves
exactly like `llmdv verify`. The CLI is an ergonomic wrapper, never a gatekeeper -- which also
means debugging the suite with native pytest, `-x` and `--pdb` stays available.

Two things here carry design weight.

**The ingress matrix is a parametrisation**, with the ingress id as the parameter id. That is
what makes `-k litellm` collapse the matrix to one column for free, and it is what lets the
same logical case run through every layer unchanged -- the precondition for attribution
meaning anything.

**Skips are classified, not bare.** `undeclared` (the fleet does not expose it),
`not_implemented` (llmdv cannot drive it yet), `requires_instrumentation` and `requires_load`
are four different facts. Collapsing them into one grey cell makes a matrix read as coverage,
so the class travels into results.jsonl with every record.
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from . import __version__
from . import surfaces as surfaces_pkg
from .cli import style
from .evidence import FailureContext, journal
from .evidence import write as write_evidence
from .ingress import adapter_for, client_for
from .log.setup import get_logger
from .output import Offloader, reference, run_prefix, whole
from .profile import known, loader
from .profile.schema import (
    CHAT_OPTIONS,
    CHAT_REASONING_EFFORTS,
    CLIENT_SESSIONS,
    MESSAGES_CHECKS,
    MESSAGES_EFFORTS,
    MESSAGES_THINKING_MODES,
    RESPONSES_OPTIONS,
    RESPONSES_REASONING_EFFORTS,
    SCENARIO_CHECKS,
    Capabilities,
    FleetPolicy,
    Ingress,
    Model,
    Profile,
    SkipClass,
)
from .results.record import ResultRecord, ResultsWriter, new_run_id
from .surfaces.base import SURFACE_FEATURES, SURFACE_PATHS, SurfaceKind

log = get_logger("plugin")

# nodeid -> live state for the case currently running, read by the reporter after the fact.
_ACTIVE: dict[str, dict[str, Any]] = {}

MARKERS = (
    "surface(name): the one API surface this case exercises",
    "surface_generic(*features): run once per drivable surface having these features",
    "case_id(id): stable identifier for this case, used as the matrix row key",
    "contract: verifies a profile declaration against reality; runs before functional suites",
    "needs(*capabilities): capabilities the case requires, skipped as undeclared when absent",
    "disruptive(reason): costs the fleet real work or sends deliberately malformed input; "
    "skipped where the profile declares the fleet shared",
)

# One marker per suite package, applied automatically, so the name a reader reaches for is a
# selector that works: `-m discovery` selects the discovery suite. Without these the package
# names look like markers and silently select nothing -- a narrower green run, which is worse
# than an error.
SUITES_DIR = Path(__file__).parent / "suites"


def suite_markers() -> tuple[str, ...]:
    """Suite package names that actually contain cases."""
    if not SUITES_DIR.is_dir():
        return ()
    return tuple(
        sorted(
            d.name
            for d in SUITES_DIR.iterdir()
            if d.is_dir() and not d.name.startswith("_") and any(d.glob("test_*.py"))
        )
    )


def known_markers() -> set[str]:
    """Every marker name `-m` can legitimately name."""
    base = {marker.split("(")[0].split(":")[0].strip() for marker in MARKERS}
    return base | set(suite_markers())


def _suite_of(item: pytest.Item) -> str | None:
    """The suite package a case lives in, from its module path."""
    module = getattr(item, "module", None)
    name = getattr(module, "__name__", "") if module else ""
    parts = name.split(".")
    if "suites" in parts:
        index = parts.index("suites")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


# ------------------------------------------------------------------ configuration


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("llmdv", "llm-d-validate functional validation")
    group.addoption(
        "--llmdv-profile", action="store", default=None, help="path to an infra-profile YAML file"
    )
    group.addoption(
        "--llmdv-ingress",
        action="append",
        default=[],
        help="restrict to these ingress ids (repeatable; a union)",
    )
    group.addoption(
        "--llmdv-client",
        action="append",
        default=[],
        help="restrict to surfaces this client needs (repeatable; a union)",
    )
    group.addoption(
        "--llmdv-surface",
        action="append",
        default=[],
        help="restrict to these surfaces (repeatable; a union)",
    )
    group.addoption(
        "--llmdv-run-dir",
        action="store",
        default=None,
        help="run directory for results.jsonl and evidence",
    )
    group.addoption(
        "--llmdv-run-id",
        action="store",
        default=None,
        help="run id to record; `llmdv verify` passes its own so every artifact agrees",
    )
    group.addoption(
        "--llmdv-kubeconfig",
        action="store",
        default=None,
        help="kubeconfig used for read-only cluster lookups",
    )
    group.addoption(
        "--llmdv-context", action="store", default=None, help="kubeconfig context override"
    )
    group.addoption(
        "--llmdv-kube-cli", action="store", default=None, help="cluster CLI executable override"
    )
    group.addoption(
        "--llmdv-started-at",
        action="store",
        default=None,
        help="UTC start of the verification window",
    )
    group.addoption(
        "--llmdv-show-checks",
        action="store_true",
        default=False,
        help="print the recorded checks under each case (what -v turns on)",
    )
    group.addoption(
        "--llmdv-transcript-cap",
        action="store",
        type=int,
        default=None,
        help="cap a stored SSE transcript at N bytes, dropping from the middle; 0 keeps everything",
    )
    group.addoption(
        "--llmdv-artifact-sink",
        action="store",
        default=None,
        help="store oversized artifacts whole in file:// or s3://, keeping the capped local copy",
    )
    group.addoption(
        "--llmdv-evidence",
        action="store",
        default="failures",
        choices=["failures", "all"],
        help="write an evidence bundle for failures only (default) or every case",
    )


def pytest_configure(config: pytest.Config) -> None:
    for marker in MARKERS:
        config.addinivalue_line("markers", marker)
    for suite in suite_markers():
        config.addinivalue_line("markers", f"{suite}: cases in the {suite} suite")

    # An -m expression naming a marker that does not exist selects nothing, and pytest reports
    # that as a successful run over an empty set. llmdv refuses the same shape elsewhere -- it
    # will not print PASSED over zero cases, and it rejects an unknown --llmdv-client -- so an
    # unknown marker name is a usage error here too, rather than a quietly narrower run.
    expression = (getattr(config.option, "markexpr", "") or "").strip()
    if expression:
        allowed = known_markers()
        named = {
            token
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expression)
            if token not in {"and", "or", "not", "True", "False", "None"}
        }
        unknown = sorted(named - allowed)
        if unknown:
            raise pytest.UsageError(
                f"-m names no such marker: {', '.join(unknown)} "
                f"(known: {', '.join(sorted(allowed))}). "
                "Use -k to select by test or parameter name instead."
            )

    path = config.getoption("--llmdv-profile")
    try:
        config._llmdv_profile = loader.load(path) if path else None  # type: ignore[attr-defined]
        if config._llmdv_profile:
            errors = [
                d for d in loader.validate_models(config._llmdv_profile) if d.level == "error"
            ]
            if errors:
                raise loader.ProfileError("\n".join(map(str, errors)))
    except loader.ProfileError as exc:
        raise pytest.UsageError(str(exc)) from None
    config._llmdv_secrets = (  # type: ignore[attr-defined]
        loader.resolve_all_secrets(config._llmdv_profile) if config._llmdv_profile else {}
    )

    run_dir = config.getoption("--llmdv-run-dir")
    # Digest of the profile file as loaded. Two runs are only comparable if they were driven
    # from the same declarations, so the matrix needs this to tell "the fleet changed" from
    # "the profile changed".
    config._llmdv_profile_digest = _digest(config._llmdv_profile)  # type: ignore[attr-defined]

    config._llmdv_run_dir = Path(run_dir) if run_dir else None  # type: ignore[attr-defined]
    config._llmdv_run_id = config.getoption("--llmdv-run-id") or new_run_id()  # type: ignore[attr-defined]
    artifact_uri = config.getoption("--llmdv-artifact-sink")
    if artifact_uri:
        from .output.remote import UnsupportedRemote, parse_remote_uri

        try:
            parse_remote_uri(artifact_uri, flag="--artifact-sink")
        except UnsupportedRemote as exc:
            raise pytest.UsageError(str(exc)) from None
    config._llmdv_offloader = Offloader(  # type: ignore[attr-defined]
        artifact_uri if config._llmdv_profile else None,
        run_prefix(config._llmdv_profile.name, config._llmdv_run_id)
        if config._llmdv_profile and artifact_uri
        else "",
    )
    config._llmdv_results = None  # type: ignore[attr-defined]
    if config._llmdv_run_dir:  # type: ignore[attr-defined]
        config._llmdv_run_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
        config._llmdv_results = ResultsWriter(  # type: ignore[attr-defined]
            config._llmdv_run_dir / "results.jsonl"  # type: ignore[attr-defined]
        )
        # Every HTTP exchange of every case, pass or fail. This is the audit trail that makes a
        # PASS checkable: without it, a green verdict is a claim with nothing behind it.
        config._llmdv_exchanges = (  # type: ignore[attr-defined]
            config._llmdv_run_dir / "exchanges.jsonl"  # type: ignore[attr-defined]
        ).open("a", encoding="utf-8", buffering=1)

    # Registered so an unredacted key can never reach a log record or an evidence file.
    from .http import redact

    redact.install_secrets(list(config._llmdv_secrets.values()))  # type: ignore[attr-defined]


def pytest_unconfigure(config: pytest.Config) -> None:
    writer = getattr(config, "_llmdv_results", None)
    if writer is not None:
        writer.close()
    handle = getattr(config, "_llmdv_exchanges", None)
    if handle is not None:
        handle.close()


# ------------------------------------------------------------------ the matrix


def _selected_ingress_ids(config: pytest.Config, profile: Profile) -> list[str]:
    chosen = config.getoption("--llmdv-ingress") or list(profile.ingress_ids)
    unknown = [item for item in chosen if item not in profile.ingress_ids]
    if unknown:
        raise pytest.UsageError(
            f"--llmdv-ingress {unknown} not in profile {profile.name!r} "
            f"(has: {', '.join(profile.ingress_ids)})"
        )
    return [item for item in profile.ingress_ids if item in chosen]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrise cases across the selected ingresses, and generic cases across surfaces."""
    if "ingress" in metafunc.fixturenames:
        _parametrise_ingress(metafunc)

    # Only a case that takes `surface` depends on one. A surface-generic module can still hold
    # surface-independent cases -- GET /v1/models, an unknown path -- and those run once.
    mark = _surface_marker(metafunc.definition)
    if mark is None or mark.name != "surface_generic" or "surface" not in metafunc.fixturenames:
        return

    unknown = set(mark.args) - SURFACE_FEATURES
    if unknown:
        raise ValueError(
            f"{metafunc.definition.nodeid}: surface_generic names unknown features "
            f"{sorted(unknown)}; known: {sorted(SURFACE_FEATURES)}"
        )
    names = drivable_surfaces(mark.args)
    if not names:
        # One skipped row that says why, where an empty parametrisation would drop the case
        # from the matrix -- and hand the reporter pytest's unserialisable NOTSET placeholder.
        reason = (
            f"[{SkipClass.NOT_IMPLEMENTED.value}] "
            f"no surface llmdv can drive has {sorted(mark.args)}"
        )
        placeholder = pytest.param(None, marks=pytest.mark.skip(reason=reason), id="no-surface")
        metafunc.parametrize("surface", [placeholder], indirect=True)
        return
    # The surface name is the id, so `-k` selects a surface the same way it selects an
    # ingress. `--surface` is exact where a name is a substring of another.
    metafunc.parametrize("surface", names, ids=names, indirect=True)


def _parametrise_ingress(metafunc: pytest.Metafunc) -> None:
    profile: Profile | None = getattr(metafunc.config, "_llmdv_profile", None)
    if profile is None:
        metafunc.parametrize("ingress", [], ids=[])
        return
    ids = _selected_ingress_ids(metafunc.config, profile)
    metafunc.parametrize(
        "ingress",
        [profile.ingress(item) for item in ids],
        ids=ids,
        # The ingress id IS the parametrisation id, so `-k router` selects one column.
        scope="session",
    )


def drivable_surfaces(features: tuple[str, ...] = ()) -> list[str]:
    """Surfaces this build can drive that have every feature asked for, in declaration order.

    Every drivable surface, not only those some ingress exposes: an ingress that does not
    expose one skips it as `undeclared` in `_gate`, which keeps the fleet gap in the matrix.
    """
    return [
        name
        for name in SURFACE_PATHS
        if name in surfaces_pkg.implemented()
        and set(features) <= surfaces_pkg.get(name).features
        and surfaces_pkg.get(name).kind
        == (SurfaceKind.RENDER if "render" in features else SurfaceKind.GENERATION)
    ]


_SURFACE_MARKERS = ("surface", "surface_generic")


def _surface_marker(node: pytest.Item) -> pytest.Mark | None:
    """The closest `surface` or `surface_generic` marker, which alone decides.

    Closest wins, as it does for any one pytest marker: a case pinned to one surface inside a
    generic module is pinned, and a generic case inside a pinned module is generic. Both on the
    same node is a contradiction, so it fails collection rather than picking one silently.
    """
    found = [
        (owner, mark)
        for owner, mark in node.iter_markers_with_node()
        if mark.name in _SURFACE_MARKERS
    ]
    if not found:
        return None
    owner, mark = found[0]
    if any(other is owner and rival.name != mark.name for other, rival in found[1:]):
        raise ValueError(f"{owner.nodeid}: marked both surface(...) and surface_generic; use one")
    return mark


def surface_of(node: pytest.Item) -> str | None:
    """The surface a case exercises: its parameter if generic, else its pinned marker."""
    callspec = getattr(node, "callspec", None)
    if callspec is not None and "surface" in callspec.params:
        return callspec.params["surface"]
    mark = _surface_marker(node)
    return mark.args[0] if mark is not None and mark.name == "surface" and mark.args else None


# Contract runs first because a stale profile makes every later verdict harder to interpret;
# discovery before the surfaces that assume the model exists. `logs` runs last, because its
# cases scan the logs everything before it produced. Everything else is alphabetical.
PHASE_ORDER = {"preflight": 0, "contract": 1, "discovery": 2, "logs": 99}


def phase_of(item: pytest.Item) -> str:
    """The suite phase a case belongs to, taken from its case_id prefix."""
    marker = item.get_closest_marker("case_id")
    if marker and marker.args and "." in marker.args[0]:
        return marker.args[0].split(".", 1)[0]
    return Path(str(getattr(item, "fspath", ""))).parent.name or "other"


def _phase_sort_key(item: pytest.Item, order: dict[str, int] | None = None) -> tuple:
    """Order: phase, then ingress (in profile order), then case.

    Grouping by ingress inside a phase lets a reader scan one layer at a time, which is how the
    attribution question is actually asked -- "did everything pass at vllm?" rather than "did
    this one case pass everywhere?". The summary table answers the latter.

    Profile order rather than alphabetical, because a profile lists ingresses in the order a
    request traverses the stack: vllm, router, litellm. Sorting alphabetically would put
    litellm first and invert the layer ladder the output is meant to make readable.
    """
    phase = phase_of(item)
    callspec = getattr(item, "callspec", None)
    ingress_id = ""
    if callspec is not None:
        for value in callspec.params.values():
            found = getattr(value, "id", None) or getattr(value, "ingress_id", None)
            if isinstance(found, str):
                ingress_id = found
                break
    rank = (order or {}).get(ingress_id, len(order or {}))
    return (PHASE_ORDER.get(phase, len(PHASE_ORDER)), phase, rank, ingress_id, item.nodeid)


def pytest_itemcollected(item: pytest.Item) -> None:
    """Tag each case with its suite.

    Done here rather than in `pytest_collection_modifyitems` because pytest applies `-m` in a
    `modifyitems` implementation of its own; a marker added there could be added after the
    filter had already run, and would select nothing.
    """
    suite = _suite_of(item)
    if suite:
        item.add_marker(suite)


# Names `needs(...)` may use, beyond the fields on Capabilities: these two are read off the
# model rather than the capability block.
_MODEL_CAPABILITIES = frozenset({"tools", "reasoning"})

# Capabilities whose value is a closed vocabulary, so `field:value` can be checked on both
# halves. A field absent here accepts any entry.
_DECLARED_VALUES = {
    "chat_options": frozenset(CHAT_OPTIONS),
    "chat_reasoning_efforts": frozenset(CHAT_REASONING_EFFORTS),
    "client_sessions": frozenset(CLIENT_SESSIONS),
    "messages_checks": frozenset(MESSAGES_CHECKS),
    "messages_efforts": frozenset(MESSAGES_EFFORTS),
    "messages_thinking": frozenset(MESSAGES_THINKING_MODES),
    "responses_options": frozenset(RESPONSES_OPTIONS),
    "responses_reasoning_efforts": frozenset(RESPONSES_REASONING_EFFORTS),
    "scenario_checks": frozenset(SCENARIO_CHECKS),
}


def _check_capability_names(items: list[pytest.Item]) -> None:
    """A `needs(...)` name that matches nothing must be a usage error, not a silent skip.

    `_has_capability` answers False for a name it does not recognise, which is exactly what it
    answers for a fleet that has not declared the capability. So a typo in llmdv's own marker --
    `messages_check:` for `messages_checks:`, or a misspelt entry -- makes every case using it
    skip as `undeclared` on every fleet, for good, and the report looks like honest coverage.

    Checked here rather than in `_has_capability` because a case that skips earlier, for a
    surface it does not run on, never reaches that function at all.
    """
    fields = {entry.name for entry in dataclasses.fields(Capabilities)}
    unknown: set[str] = set()
    for item in items:
        for marker in item.iter_markers("needs"):
            for name in marker.args:
                if name in _MODEL_CAPABILITIES:
                    continue
                field, _, entry = name.partition(":")
                if field not in fields:
                    unknown.add(name)
                elif entry and entry not in _DECLARED_VALUES.get(field, {entry}):
                    unknown.add(name)
    if unknown:
        raise pytest.UsageError(
            f"needs() names no such capability: {', '.join(sorted(unknown))}. "
            f"Known capabilities: {', '.join(sorted(fields | _MODEL_CAPABILITIES))}."
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply --llmdv-client and --llmdv-surface as intersecting filters.

    Selectors narrow; they never widen. A case with no surface marker is infrastructure (the
    contract suite, for instance) and is never filtered out by a surface selector.
    """
    _check_capability_names(items)

    profile: Profile | None = getattr(config, "_llmdv_profile", None)
    if profile is None:
        return

    wanted: set[str] = set()
    for client_id in config.getoption("--llmdv-client") or []:
        try:
            wanted |= set(profile.client(client_id).surfaces)
        except KeyError:
            raise pytest.UsageError(
                f"--llmdv-client {client_id!r} not in profile {profile.name!r} "
                f"(has: {', '.join(item.id for item in profile.clients) or 'none'})"
            ) from None
    for surface in config.getoption("--llmdv-surface") or []:
        wanted.add(surface)

    order = {ingress_id: index for index, ingress_id in enumerate(profile.ingress_ids)}
    if not wanted:
        items.sort(key=lambda item: _phase_sort_key(item, order))
        return

    kept, deselected = [], []
    for item in items:
        surface = surface_of(item)
        if surface is None or surface in wanted:
            kept.append(item)
        else:
            deselected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept
    items.sort(key=lambda item: _phase_sort_key(item, order))


# ------------------------------------------------------------------ fixtures


def pytest_runtest_logstart(nodeid: str, location) -> None:
    """Start a fresh check journal for each case."""
    journal.activate(nodeid)


def pytest_runtest_logfinish(nodeid: str, location) -> None:
    journal.deactivate()


def pytest_collection_finish(session: pytest.Session) -> None:
    """Count the cases in each phase, so a heading can state its size before it runs."""
    sizes: dict[str, int] = {}
    for item in session.items:
        phase = phase_of(item)
        sizes[phase] = sizes.get(phase, 0) + 1
    session.config._llmdv_phase_sizes = sizes  # type: ignore[attr-defined]
    session.config._llmdv_phase_seen = None  # type: ignore[attr-defined]
    if not session.items:
        log.warning("no cases collected -- check --ingress/--client/--surface and -k/-m")


def _phase_heading(config: pytest.Config, phase: str) -> None:
    """Emit a heading the first time a phase produces a result."""
    if getattr(config, "_llmdv_phase_seen", None) == phase:
        return
    config._llmdv_phase_seen = phase  # type: ignore[attr-defined]

    rich = style.rich_stderr()
    palette = style.Palette.for_stream(rich)
    size = getattr(config, "_llmdv_phase_sizes", {}).get(phase, 0)
    sep = "\u00b7" if rich else "-"
    label = f"{phase} {sep} {size} case{'s' if size != 1 else ''}"
    bar = style.rule_char(rich)
    log.info("")
    log.info("%s", palette.paint(f"{bar * 2} {label} {bar * max(4, 64 - len(label))}", palette.dim))


@pytest.fixture(scope="session")
def profile(request: pytest.FixtureRequest) -> Profile:
    found: Profile | None = getattr(request.config, "_llmdv_profile", None)
    if found is None:
        pytest.skip("no --llmdv-profile given")
    return found


@pytest.fixture(scope="session")
def model(ingress: Ingress) -> Model:
    """The selected ingress already carries its resolved model."""
    return ingress.model


def _digest(profile: Profile | None) -> str | None:
    """Short digest of the profile's declarations, or None when there is no file to read."""
    if profile is None or not profile.source_path or not profile.source_path.is_file():
        return None
    return loader.contract_digest(profile.source_path)


@pytest.fixture(scope="session")
def profile_digest(request: pytest.FixtureRequest) -> str | None:
    return getattr(request.config, "_llmdv_profile_digest", None)


@pytest.fixture
def surface(request: pytest.FixtureRequest):
    """The Surface implementation this case exercises: its parameter, or its pinned marker."""
    name = surface_of(request.node)
    if name is None:
        pytest.fail(
            "this fixture requires @pytest.mark.surface(<name>) or @pytest.mark.surface_generic"
        )
    return surfaces_pkg.get(name)


@pytest.fixture(autouse=True)
def _gate(request: pytest.FixtureRequest) -> None:
    """Skip, with a classified reason, anything this fleet or this build cannot run.

    Runs before the client is built so an undeclared surface costs no connection. The reason
    string is what distinguishes a fleet gap from a tool gap in the results.
    """
    # Policy first, and deliberately before the surface check: a shared fleet is other people's,
    # and a case that costs it real work or puts deliberately malformed input into its logs should
    # be declined before a connection is opened. The row stays in the matrix as
    # excluded_by_policy rather than vanishing, because a missing row reads as coverage.
    disruptive = request.node.get_closest_marker("disruptive")
    if disruptive:
        policy: Profile = request.getfixturevalue("profile")
        if policy.fleet_policy is FleetPolicy.SHARED:
            why = (disruptive.args[0] if disruptive.args else "") or "is disruptive"
            _mark_skip(request.node, SkipClass.EXCLUDED_BY_POLICY)
            pytest.skip(
                f"[{SkipClass.EXCLUDED_BY_POLICY.value}] fleet_policy is shared and this case {why}"
            )

    name = surface_of(request.node)
    if name is None:
        return

    target = None
    if "ingress" in request.fixturenames:
        target = request.getfixturevalue("ingress")
        if not target.exposes(name):
            _mark_skip(request.node, SkipClass.UNDECLARED)
            pytest.skip(
                f"[{SkipClass.UNDECLARED.value}] ingress {target.id!r} does not "
                f"expose surface {name!r}"
            )

    if name not in surfaces_pkg.implemented():
        _mark_skip(request.node, SkipClass.NOT_IMPLEMENTED)
        pytest.skip(
            f"[{SkipClass.NOT_IMPLEMENTED.value}] surface {name!r} is declared by this "
            f"fleet but llmdv {__version__} cannot drive it yet"
        )

    needs = request.node.get_closest_marker("needs")
    if needs:
        declared = target or request.getfixturevalue("profile")
        where = f"ingress {target.id!r}" if target else "profile"
        for capability in needs.args:
            if not _has_capability(declared, capability, name):
                _mark_skip(request.node, SkipClass.UNDECLARED)
                pytest.skip(
                    f"[{SkipClass.UNDECLARED.value}] {where} does not declare "
                    f"capability {capability!r} for surface {name!r}"
                )


def _has_capability(declared: Ingress | Profile, name: str, surface: str) -> bool:
    if name == "tools":
        return declared.model.tools.supported
    if name == "reasoning":
        return declared.model.reasoning.parser is not None or declared.model.reasoning.is_inline
    # "field:value" asks for one entry of a capability that is a list of names.
    if ":" in name:
        field, _, entry = name.partition(":")
        return entry in getattr(declared.capabilities, field, ())
    value = getattr(declared.capabilities, name, False)
    return surface in value if isinstance(value, tuple) else bool(value)


def _correlation_headers(config: pytest.Config, node: pytest.Item) -> dict[str, str]:
    """Headers shared by all clients belonging to one selected case."""
    marker = node.get_closest_marker("case_id")
    case_id = marker.args[0] if marker and marker.args else node.nodeid
    return {
        "x-llmdv-run-id": getattr(config, "_llmdv_run_id", "unknown"),
        "x-llmdv-case-id": case_id,
    }


@pytest.fixture
def client(request: pytest.FixtureRequest, ingress: Ingress):
    """A recording HTTP client bound to one ingress, tracked for evidence capture.

    Registering the exchange list before the request runs is what lets the reporter write an
    evidence bundle for a failure it only learns about afterwards.
    """
    secrets: dict[str, str] = getattr(request.config, "_llmdv_secrets", {})
    # Headers the surface's wire shape requires, e.g. `anthropic-version`, so a case never has
    # to know them -- forgetting one would make every request on that surface fail alike.
    name = surface_of(request.node)
    http = client_for(
        ingress,
        secret=secrets.get(ingress.id),
        extra_headers=surfaces_pkg.get(name).extra_headers() if name else None,
        correlation_headers=_correlation_headers(request.config, request.node),
        request_id_factory=lambda: f"llmdv-{uuid.uuid4().hex[:12]}",
        transcript_cap=request.config.getoption("--llmdv-transcript-cap"),
    )
    register_client(request.node, ingress, http)
    try:
        yield http
    finally:
        http.close()


@pytest.fixture
def client_factory(request: pytest.FixtureRequest, profile: Profile):
    """Build a client for an ingress named at runtime, rather than one parametrised in.

    The contract suite's header cases already carry an ingress id in their own parameters.
    Having them also depend on the parametrised `ingress` fixture would form a cross-product
    whose off-diagonal cells exist only to skip themselves -- noise in the matrix and wasted
    collection. This gives those cases a client without that second axis.
    """
    secrets: dict[str, str] = getattr(request.config, "_llmdv_secrets", {})
    built: list[Any] = []

    def make(ingress_id: str):
        target = profile.ingress(ingress_id)
        http = client_for(
            target,
            secret=secrets.get(target.id),
            correlation_headers=_correlation_headers(request.config, request.node),
            request_id_factory=lambda: f"llmdv-{uuid.uuid4().hex[:12]}",
            transcript_cap=request.config.getoption("--llmdv-transcript-cap"),
        )
        built.append(http)
        # Registered exactly as the `client` fixture does, so a failure here still produces an
        # evidence bundle with the right ingress attached.
        register_client(request.node, target, http)
        return http

    try:
        yield make
    finally:
        for http in built:
            http.close()


def register_client(node: pytest.Item, ingress: Ingress, http: Any) -> None:
    """Attach a client to the running case so the reporter can collect its evidence.

    Anything exposing `exchanges` and `last` works, which is how an SDK-backed recorder gets
    the same treatment as llmdv's own client.
    """
    case_marker = node.get_closest_marker("case_id")
    state = _ACTIVE.setdefault(node.nodeid, {})
    # Every client, in the order they were built. A case that talks to two ingresses -- the
    # cross-layer prompt-size comparison does -- registers twice, and `client` is the one the
    # verdict belongs to. Keeping only that one lost the other side's exchanges from
    # exchanges.jsonl and from the failure bundle, which is precisely the evidence a layer
    # comparison exists to produce.
    clients = state.setdefault("clients", [])
    if not any(existing is http for existing in clients):
        clients.append(http)
    state.setdefault("client_ingresses", {})[id(http)] = ingress
    state.update(
        {
            "ingress": ingress,
            "surface": surface_of(node),
            "case_id": case_marker.args[0] if case_marker and case_marker.args else None,
            "client": http,
        }
    )


def register_artifact(nodeid: str, name: str, text: str) -> None:
    """Attach a file to this case's evidence bundle instead of to a check.

    Some evidence is too big for `results.jsonl` and too useful to drop: a tool-using client's
    event trace, the project a session edited. A check carries its payload into results.jsonl,
    which `llmdv compare` reads back for every run in a window, so a multi-megabyte observation
    there is paid for on every comparison. A bundle file is written once, beside the verdict.

    Callers clamp before calling; this does not guess a limit on their behalf.
    """
    _ACTIVE.setdefault(nodeid, {}).setdefault("artifacts", {})[name] = text


def _mark_skip(node: pytest.Item, skip_class: SkipClass) -> None:
    _ACTIVE.setdefault(node.nodeid, {})["skip_class"] = skip_class.value


def mark_skip_current(skip_class: SkipClass) -> None:
    """Classify a skip raised from inside an assertion, which has no pytest node to hand.

    Uses the journal's notion of the running case rather than threading a node through every
    assertion signature. A skip raised outside a case silently records nothing, which is correct:
    there is no row for it to classify.
    """
    active = journal.current()
    if active is not None:
        _ACTIVE.setdefault(active.nodeid, {})["skip_class"] = skip_class.value


# ------------------------------------------------------------------ reporting


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" and not (report.when == "setup" and report.outcome == "skipped"):
        return

    config = item.config
    profile: Profile | None = getattr(config, "_llmdv_profile", None)
    writer: ResultsWriter | None = getattr(config, "_llmdv_results", None)
    state = _ACTIVE.get(item.nodeid, {})
    state["item"] = item

    ingress: Ingress | None = state.get("ingress")
    # A case that never built a client -- the pinned-SDK cases use their own -- is still
    # parametrised over ingresses, so recover the ingress from the parametrisation. Without
    # this those rows land in the matrix with no column and look like they did not run.
    if ingress is None:
        ingress = _ingress_from_params(item, profile)
    http = state.get("client")
    case_marker = item.get_closest_marker("case_id")
    surface = surface_of(item)

    served_by: dict[str, Any] = {}
    http_status = None
    if http is not None and http.last is not None:
        http_status = http.last.status
        if ingress is not None:
            served_by = adapter_for(ingress.kind).served_by(http.last).to_dict()

    active = journal.current()
    checks = [check.to_dict() for check in active.checks] if active else []

    # The profile's triage of this result, if it has one. Computed from the same checks the
    # record carries, so a reader can verify the label against the evidence beside it.
    known_failure = None
    if profile is not None and profile.known_failures:
        known_failure = known.classify(
            profile.known_failures,
            outcome=_outcome(report),
            case_id=case_marker.args[0] if case_marker and case_marker.args else None,
            variant=_variant(item),
            ingress=ingress.id if ingress else None,
            checks=checks,
            signals=_build_signals(state, http, ingress),
        )

    # The audit trail for every case, not just the failures. A reviewer asking "did that pass
    # really talk to the fleet, and what came back?" answers it from here.
    seq = 0
    client_ingresses = state.get("client_ingresses", {})
    recorded_clients = state.get("clients") or ([http] if http is not None else [])
    from .evidence.bundle import case_slug

    offload_seq = 0
    for recorded in recorded_clients:
        owner = client_ingresses.get(id(recorded), ingress)
        for exchange in recorded.exchanges:
            if exchange.transcript_full is not None and exchange.transcript_ref is None:
                data = exchange.transcript_full.encode("utf-8")
                kept_bytes = len((exchange.response_text or "").encode("utf-8"))
                case_name = case_marker.args[0] if case_marker and case_marker.args else item.nodeid
                slugged_case = case_slug(
                    owner.id if owner else "noingress", case_name, _variant(item)
                )
                relpath = f"transcripts/{slugged_case}/{offload_seq}.sse"
                offload = config._llmdv_offloader.store(  # type: ignore[attr-defined]
                    relpath, data
                )
                exchange.transcript_ref = reference(data, kept_bytes, offload)
                exchange.transcript_full = None
            offload_seq += 1
        # The sequence for exchanges must include non-transcript exchanges too.
        seq = _write_exchanges(
            config,
            item,
            client_ingresses.get(id(recorded), ingress),
            recorded,
            surface,
            case_marker,
            seq,
        )

    partial_evidence = any(
        exchange.transcript_ref is not None and not whole(exchange.transcript_ref)
        for recorded in recorded_clients
        for exchange in recorded.exchanges
    )
    evidence_ref = None
    run_dir: Path | None = getattr(config, "_llmdv_run_dir", None)
    want_all = config.getoption("--llmdv-evidence") == "all"
    if (report.failed or want_all) and run_dir is not None:
        evidence_ref = write_evidence(
            run_dir,
            FailureContext(
                nodeid=item.nodeid,
                case_id=case_marker.args[0] if case_marker and case_marker.args else None,
                ingress=ingress,
                surface=surface,
                exchanges=[
                    exchange
                    for recorded in (state.get("clients") or ([http] if http is not None else []))
                    for exchange in recorded.exchanges
                ],
                artifacts=dict(state.get("artifacts") or {}),
                served_by=served_by,
                setup_commands=getattr(config, "_llmdv_setup_commands", {}).get(
                    ingress.id if ingress else "", []
                ),
                captured_output=(report.capstdout or "") + (report.capstderr or ""),
                longrepr=str(report.longrepr or ""),
                checks=checks,
                outcome=_outcome(report),
                variant=_variant(item),
                known_failure=known_failure,
            ),
        )

    _log_progress(report, state, ingress, case_marker, active, item, known_failure)

    if writer is not None and profile is not None:
        writer.append(
            ResultRecord(
                run_id=getattr(config, "_llmdv_run_id", "unknown"),
                nodeid=item.nodeid,
                outcome=_outcome(report),
                profile=profile.name,
                profile_digest=getattr(config, "_llmdv_profile_digest", None),
                case_id=case_marker.args[0] if case_marker and case_marker.args else None,
                variant=_variant(item),
                clients=_clients_for_surface(profile, surface),
                ingress=ingress.id if ingress else None,
                ingress_kind=ingress.kind.value if ingress else None,
                surface=surface,
                skip_class=state.get("skip_class") or _skip_class_from_reason(_reason(report)),
                reason=_reason(report),
                duration_s=round(report.duration, 4),
                http_status=http_status,
                served_by=served_by,
                checks=checks,
                evidence=evidence_ref,
                known_failure=known_failure,
                partial_evidence=partial_evidence,
            )
        )
    _ACTIVE.pop(item.nodeid, None)


def _build_signals(state: dict, http, ingress: Ingress | None) -> list[tuple[str | None, dict]]:
    """The build signals each of this case's responses carried, with the ingress each came
    from, named by that ingress's own adapter: what a `known_failures` entry's `until` is
    judged against. A cross-layer case holds clients for two ingresses, so the per-client map
    decides which adapter reads which response."""
    client_ingresses = state.get("client_ingresses", {})
    out: list[tuple[str | None, dict]] = []
    for recorded in state.get("clients") or ([http] if http is not None else []):
        owner: Ingress | None = client_ingresses.get(id(recorded), ingress)
        if owner is None:
            continue
        adapter = adapter_for(owner.kind)
        for exchange in recorded.exchanges:
            headers = {
                str(name).lower(): str(value)
                for name, value in (exchange.response_headers or {}).items()
            }
            carried = adapter.version_signals(headers, exchange.response_body)
            if carried:
                out.append((owner.id, carried))
    return out


_SKIP_CLASS_PREFIX = re.compile(r"\[(" + "|".join(item.value for item in SkipClass) + r")\]")


def _skip_class_from_reason(reason: str | None) -> str | None:
    """Recover the skip class from a reason string beginning "[<class>] ...".

    The gate fixture registers the class directly, but a case that decides to skip in its own
    body -- the header-disposition case, for instance -- would otherwise land in the matrix as a
    bare skip. Parsing the documented prefix means the convention is enough; no test has to
    remember a second registration call for its class to reach results.jsonl.
    """
    if not reason:
        return None
    found = _SKIP_CLASS_PREFIX.search(reason)
    return found.group(1) if found else None


VERDICT = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP", "error": "ERR "}


def _ingress_from_params(item: pytest.Item, profile: Profile | None) -> Ingress | None:
    """Recover a case's ingress from its parametrisation.

    Two shapes need this. The pinned-SDK cases are parametrised over `ingress` but build their
    own client, so nothing registers one. The header cases are not parametrised over `ingress` at
    all and carry it inside a `HeaderCase` -- and they may skip before building a client, which
    without this would put them in no column and read as "did not run".
    """
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return None
    for value in callspec.params.values():
        if isinstance(value, Ingress):
            return value
        named = getattr(value, "ingress_id", None)
        if named and profile is not None:
            try:
                return profile.ingress(named)
            except KeyError:
                continue
    return None


def _variant(item: pytest.Item) -> str | None:
    """A case's parametrisation other than the ingress, or None when there is none.

    The ingress already has its own column wherever a case is shown, so repeating it would only
    pad the line. What is wanted is the discriminator: which header a disposition case checked,
    so six header cases read as six rows rather than one repeated label.

    The parameter VALUES are read rather than pytest's generated id. The id joins parameters
    with "-", and these parameters are header names that contain "-" themselves, so parsing the
    id turned `x-llm-d-inference-fairness-id` into `x/llm/d/inference/fairness/id`.
    """
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return None

    parts: list[str] = []
    for name, value in callspec.params.items():
        if name == "ingress":
            continue
        if getattr(value, "header", None):
            # A HeaderCase: the header is the discriminator; its ingress and disposition are
            # already shown in their own column and in the skip reason respectively.
            parts.append(str(value.header))
        elif isinstance(value, tuple) and value:
            parts.append(str(value[0]))
        elif isinstance(value, (str, int, float)):
            parts.append(str(value))
    return "/".join(parts) or None


def _write_exchanges(
    config: pytest.Config,
    item: pytest.Item,
    ingress: Ingress | None,
    http: Any,
    surface: str | None,
    case_marker: pytest.Mark | None,
    start: int = 0,
) -> int:
    """Append every HTTP exchange this case made to exchanges.jsonl, already redacted.

    Returns the next sequence number, so a case that registered more than one client numbers
    its rows continuously rather than restarting at zero for each.
    """
    handle = getattr(config, "_llmdv_exchanges", None)
    if handle is None or http is None:
        return start
    from .http import redact  # local, as at registration: plugin import order matters

    index = start
    for exchange in http.exchanges:
        handle.write(
            json.dumps(
                redact.redact_value(
                    {
                        "run_id": getattr(config, "_llmdv_run_id", None),
                        "nodeid": item.nodeid,
                        "case_id": case_marker.args[0]
                        if case_marker and case_marker.args
                        else None,
                        "ingress": ingress.id if ingress else None,
                        "ingress_kind": ingress.kind.value if ingress else None,
                        "surface": surface,
                        "seq": index,
                        **exchange.to_dict(),
                    }
                )
            )
            + "\n"
        )
        index += 1
    return index


def _log_progress(
    report: pytest.TestReport,
    state: dict[str, Any],
    ingress: Ingress | None,
    case_marker: pytest.Mark | None,
    active: journal.Journal | None = None,
    item: pytest.Item | None = None,
    known_failure: dict[str, Any] | None = None,
) -> None:
    """One line per case: verdict, ingress, case, and timing or skip reason.

    A skip prints its class instead of a duration, because "why did this not run" is the
    question a skipped case actually raises. The recorded checks are printed underneath only
    with --llmdv-show-checks (which -v sets); they are always in results.jsonl and llmdv.log, so
    keeping them off by default costs no evidence and keeps the column alignment intact.
    """
    outcome = _outcome(report)
    config = item.config if item is not None else None
    if config is not None:
        _phase_heading(config, phase_of(item))

    palette = style.Palette.for_stream(style.rich_stderr())

    # The phase heading already names the phase, so the case line drops the redundant prefix.
    full = (
        case_marker.args[0] if case_marker and case_marker.args else report.nodeid.split("::")[-1]
    )
    case = full.split(".", 1)[1] if "." in full else full
    variant = _variant(item) if item is not None else None
    if variant:
        case = f"{case} [{variant}]"

    detail = state.get("skip_class") or _skip_class_from_reason(_reason(report) or "") or "skipped"
    if outcome != "skip":
        detail = f"{report.duration:.2f}s"
    status = (known_failure or {}).get("status")
    if status == known.KNOWN:
        detail += f"  known: {known_failure['issue']}"
    elif status == known.FIX_EXPECTED:
        detail += f"  fix for {known_failure['issue']} expected on this build, still failing"

    log.info(
        "   %s %-8s %-52s %s",
        style.verdict(outcome, palette, width=5),
        palette.paint(ingress.id if ingress else "-", palette.dim),
        case[:52],
        palette.paint(detail, palette.dim),
    )

    if active and active.checks and config is not None and config.getoption("--llmdv-show-checks"):
        for check in active.checks:
            mark = "" if check.ok else palette.red
            log.info("        %s", palette.paint(check.render(), mark or palette.dim))


def _outcome(report: pytest.TestReport) -> str:
    if report.skipped:
        return "skip"
    if report.failed:
        return "fail" if report.when == "call" else "error"
    return "pass"


def _reason(report: pytest.TestReport) -> str | None:
    """The reason a case did not pass, as the one line worth putting in a matrix cell.

    pytest's `longrepr` leads with fixture values and the source of the failing function and
    ends with the actual message, so head-truncating it keeps the context and throws away the
    finding. `reprcrash.message` is that final message directly. The full traceback is still
    written to assertion.txt in the evidence bundle, so nothing is lost by preferring it here.
    """
    if report.skipped and isinstance(report.longrepr, tuple):
        return str(report.longrepr[2])
    if report.failed:
        crash = getattr(report.longrepr, "reprcrash", None)
        if crash is not None and getattr(crash, "message", None):
            return str(crash.message)[:2000]
        return str(report.longrepr or "")[-2000:]
    return None


def _clients_for_surface(profile: Profile, surface: str | None) -> list[str]:
    """Which declared clients depend on this case's surface.

    Derived rather than annotated: a client's dependencies live in the profile, so a test
    never has to be edited when a client's needs change.
    """
    if surface is None:
        return []
    return [client.id for client in profile.clients if surface in client.surfaces]
