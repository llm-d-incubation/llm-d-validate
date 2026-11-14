"""`llmdv profile validate` and `llmdv profile show`.

Both are offline. No credential is resolved and no endpoint is contacted, so a profile can be
written and checked from anywhere -- the whole point of keeping reachability in preflight.

`show --setup` is the exception that uses a kubeconfig, and only to read: it resolves a
requirement's label selector to a live pod name so the printed port-forward command works now,
rather than naming a pod that was running when the profile was written.
"""

from __future__ import annotations

import argparse

from ..kube import KubeContext, KubeError
from ..log.setup import emit, get_logger
from ..preflight import _command_for
from ..profile import loader
from ..surfaces.base import SURFACE_PATHS
from .contract_display import contract_lines
from .exit_codes import OK, USAGE

log = get_logger("cli.profile")


def add_parser(subparsers, parents) -> None:
    profile = subparsers.add_parser(
        "profile", parents=parents, help="inspect and validate an infra-profile"
    )
    actions = profile.add_subparsers(dest="profile_action", required=True)

    validate = actions.add_parser(
        "validate", parents=parents, help="offline schema and consistency checks"
    )
    validate.add_argument("path", help="path to the profile YAML")
    validate.set_defaults(func=cmd_validate)

    show = actions.add_parser("show", parents=parents, help="print the resolved profile")
    show.add_argument("path", help="path to the profile YAML")
    show.add_argument(
        "--setup",
        action="store_true",
        help="print the setup commands for each ingress, resolving selectors live",
    )
    show.add_argument(
        "--matrix",
        action="store_true",
        help="print the case matrix this profile implies, including what will skip",
    )
    show.set_defaults(func=cmd_show)


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        profile = loader.load(args.path)
    except loader.ProfileError as exc:
        log.error("%s", exc)
        return USAGE

    diagnostics = loader.validate(profile)
    errors = [item for item in diagnostics if item.level == "error"]
    warnings = [item for item in diagnostics if item.level == "warning"]

    for item in diagnostics:
        emit(str(item))

    emit("")
    emit(f"{profile.name}: {len(errors)} error(s), {len(warnings)} warning(s)")
    return USAGE if errors else OK


def cmd_show(args: argparse.Namespace) -> int:
    try:
        profile = loader.load(args.path)
    except loader.ProfileError as exc:
        log.error("%s", exc)
        return USAGE

    if args.setup:
        return _show_setup(args, profile)
    if args.matrix:
        return _show_matrix(profile)

    emit(f"profile   {profile.name}  (schema {profile.schema})")
    emit(f"vantage   {profile.vantage.value}")
    if profile.environment.namespace:
        emit(f"cluster   {profile.environment.cluster or '-'} / ns {profile.environment.namespace}")
        emit(f"context   {profile.environment.context or '-'}")
    emit("")
    reasoning = profile.model.reasoning
    mode = (
        f"parser={reasoning.parser}"
        if reasoning.parser
        else f"inline <{reasoning.inline_tag}>"
        if reasoning.is_inline
        else "none"
    )
    model_label = (
        "defaults"
        if any(
            i.model != profile.model or i.capabilities != profile.capabilities
            for i in profile.ingresses
        )
        else "model"
    )
    emit(f"{model_label:<10}{profile.model.public_id}")
    emit(
        f"          max_model_len={profile.model.max_model_len}  reasoning={mode}"
        f"  tools={'yes' if profile.model.tools.supported else 'no'}"
    )
    if profile.model.answer_max_tokens is not None:
        emit(f"          answer_max_tokens={profile.model.answer_max_tokens}")
    emit("")
    emit("ingresses")
    for ingress in profile.ingresses:
        auth = ingress.auth.type.value
        if ingress.auth.key_env:
            auth += f" (${ingress.auth.key_env})"
        emit(f"  {ingress.id:<10} {ingress.kind.value:<13} {ingress.base_url}")
        emit(f"  {'':<10} auth={auth}  surfaces={', '.join(ingress.surfaces) or 'none'}")
        for line in contract_lines(ingress.model, ingress.capabilities):
            emit(f"  {'':<10} {line}")
    emit("")
    emit("clients")
    for client in profile.clients:
        emit(
            f"  {client.id:<14} surfaces={', '.join(client.surfaces) or 'none'}"
            f"  needs={', '.join(client.needs) or 'none'}"
        )
    if profile.headers:
        emit("")
        emit("headers")
        for header, per_ingress in sorted(profile.headers.items()):
            cells = "  ".join(f"{k}={v.value}" for k, v in sorted(per_ingress.items()))
            emit(f"  {header:<32} {cells}")
    if profile.known_failures:
        emit("")
        emit("known failures  (labels on failures of these checks; the verdict is unchanged)")
        for entry in profile.known_failures:
            scope = ", ".join(entry.cases) or "any case"
            layers = ", ".join(entry.ingresses) or "any ingress"
            until = ""
            if entry.until:
                until = "  until " + " ".join(f"{k}>={v}" for k, v in entry.until.items())
            emit(f"  {entry.check:<32} {entry.issue}  [{scope} @ {layers}]{until}")
            if entry.fix:
                emit(f"  {'':<32} fix: {entry.fix}")
    return OK


def _show_setup(args: argparse.Namespace, profile) -> int:
    kube = KubeContext(
        kubeconfig=args.kubeconfig or profile.environment.kubeconfig,
        context=args.context or profile.environment.context,
        namespace=profile.environment.namespace,
        cli=getattr(args, "kube_cli", None),
    )
    live = kube.available
    if not live:
        log.warning("%s; selectors will be printed unresolved", kube.unavailable_reason)

    emit(f"# setup for {profile.name} ({profile.vantage.value})")
    emit("# run these, then: llmdv verify --profile <this file>")
    emit("")
    for ingress in profile.ingresses:
        if not ingress.setup.requires:
            emit(f"# {ingress.id}: no setup required ({ingress.base_url})")
            continue
        emit(f"# {ingress.id} -> {ingress.base_url}")
        for requirement in ingress.setup.requires:
            try:
                emit(_command_for(requirement, profile, kube))
            except KubeError as exc:
                log.warning("could not resolve a requirement for %s: %s", ingress.id, exc)
        emit("")
    return OK


def _show_matrix(profile) -> int:
    """Print what will run, and what will not, before anything is contacted."""
    from ..surfaces import implemented

    built = implemented()
    emit(f"# case matrix implied by {profile.name}")
    emit("")
    header = f"{'surface':<20} " + " ".join(f"{i:<22}" for i in profile.ingress_ids)
    emit(header)
    emit("-" * len(header))
    for surface in sorted(SURFACE_PATHS):
        cells = []
        for ingress_id in profile.ingress_ids:
            ingress = profile.ingress(ingress_id)
            if not ingress.exposes(surface):
                cells.append("skip:undeclared")
            elif surface not in built:
                cells.append("skip:not_implemented")
            else:
                cells.append("RUN")
        emit(f"{surface:<20} " + " ".join(f"{c:<22}" for c in cells))
    emit("")
    emit("skip:undeclared      = the fleet does not expose it; nothing is wrong")
    emit("skip:not_implemented = llmdv cannot drive it yet; a gap in the tool, not the fleet")
    emit("")
    emit("effective contracts (capability gates use these declarations)")
    for ingress in profile.ingresses:
        emit(f"  {ingress.id}")
        for line in contract_lines(ingress.model, ingress.capabilities):
            emit(f"    {line}")
    return OK
