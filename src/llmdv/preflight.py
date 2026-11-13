"""Preflight: separate "your vantage point is wrong" from "the fleet is broken".

This is the reason llmdv has a distinct exit code 3. A connection refused against localhost is
almost never a fleet defect -- it is a port-forward that was never started, or that died, or
that someone else's process is holding. Letting that surface as a test failure teaches people
to distrust the suite, so preflight runs before collection and refuses to start.

Seven classes of problem are caught here:

    base_url      a selected ingress takes base_url_env from a variable that is not set, or
                  that holds no https URL on the default port while the ingress declares
                  transport.http_redirect
    credentials   a key_env variable that is not set, or a key the ingress rejects (401)
    proxy         `proxy: bypass` while HTTP_PROXY would capture the request anyway
    reachability  nothing answering, or something answering that is not the fleet
    access        the ingress answers, but its model list does not offer the profile's model
                  to this key: every case would fail on it, so the run stops in seconds
    output        the --remote-output destination is not writable
    environment   cluster access, pod logs or a metrics source the run needs is unavailable
                  (only a warning when it limits the evidence rather than the cases)

The proxy check earns its place: the forge harness already documents that over the VPN every
request shares one proxy hop, and an unnoticed hop produces plausible, wrong results rather
than an error. That is worse than a failure.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit

from .http.client import RecordingClient
from .http.redact import scrub
from .kube import NO_CLI_REASON, KubeContext, KubeError
from .log.setup import get_logger
from .output.remote import UnsupportedRemote, parse_remote_uri, sink_for
from .profile.loader import resolve_secret
from .profile.schema import AuthType, Ingress, Profile, ProxyMode, Requirement
from .profile.transport import plain_http_url

log = get_logger("preflight")


class LogAccess(Enum):
    """Whether this run can collect a layer's own account, and if not, whose problem that is.

    One verdict rather than a boolean because the fixes differ, and because the states used to
    be indistinguishable from the outside: all of them produced an empty `logs/` directory
    after the run had already announced it was collecting.
    """

    AVAILABLE = "available"
    NOT_DECLARED = "not_declared"  # no selected ingress declares a log source
    NO_NAMESPACE = "no_namespace"  # declared, but the profile names no namespace to look in
    NO_CLUSTER_CLI = "no_kubectl"  # retain the legacy value for compatibility
    DENIED = "denied"  # pods/log is forbidden with this kubeconfig
    UNKNOWN = "unknown"  # the probe was inconclusive, or preflight was skipped

    @property
    def reason(self) -> str:
        return _LOG_ACCESS_REASON[self]


_LOG_ACCESS_REASON = {
    LogAccess.AVAILABLE: "pod logs are collectable",
    LogAccess.NOT_DECLARED: "no selected ingress declares a log source",
    LogAccess.NO_NAMESPACE: "the profile declares log sources but no namespace to find them in",
    LogAccess.NO_CLUSTER_CLI: NO_CLI_REASON,
    LogAccess.DENIED: "reading pod logs is forbidden with this kubeconfig",
    LogAccess.UNKNOWN: "cluster access could not be determined",
}


@dataclass
class Problem:
    ingress_id: str | None
    kind: str  # base_url | credentials | proxy | reachability | access | environment | output
    message: str
    remedy: str | None = None

    def render(self) -> str:
        where = f"[{self.ingress_id}] " if self.ingress_id else ""
        text = f"{where}{self.kind}: {self.message}"
        if self.remedy:
            text += f"\n    remedy: {self.remedy}"
        return text


@dataclass
class Report:
    problems: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)
    reachable: set[str] = field(default_factory=set)
    resolved_setup: dict[str, list[str]] = field(default_factory=dict)
    # Health-probe round trip per ingress. Shown in the preflight section, and worth having:
    # a tunnel that answers in 400ms rather than 40ms is usually the first sign of a proxy hop.
    latency_ms: dict[str, float] = field(default_factory=dict)
    # Decided here rather than after the suite, so "the evidence you asked for is not
    # obtainable" is known before the first request instead of at minute forty.
    log_access: LogAccess = LogAccess.UNKNOWN
    log_access_reason: str = LogAccess.UNKNOWN.reason

    @property
    def ok(self) -> bool:
        return not self.problems


def run(
    profile: Profile,
    ingress_ids: list[str] | None = None,
    kube: KubeContext | None = None,
    remote_output: str | None = None,
    artifact_sink: str | None = None,
    require_logs: bool = False,
) -> Report:
    """Check everything needed before the first test. Never mutates anything."""
    report = Report()
    selected = ingress_ids or list(profile.ingress_ids)

    for uri, area in ((remote_output, "output"), (artifact_sink, "artifact_sink")):
        if not uri:
            continue
        # Fail here rather than at publish time: discovering an unusable destination after a
        # long suite means the results have nowhere to go.
        try:
            parse_remote_uri(
                uri,
                flag="--artifact-sink" if area == "artifact_sink" else "--remote-output",
            )
            sink_for(uri).check()
        except UnsupportedRemote as exc:
            report.problems.append(Problem(None, area, scrub(str(exc))))
        except Exception as exc:  # noqa: BLE001 -- any storage error means "not writable"
            report.problems.append(Problem(None, area, scrub(f"{uri} is not writable: {exc}")))

    # Only a warning: cluster access serves selectors and pod logs, never the tests themselves.
    environment_declared = any(
        (
            profile.environment.cluster,
            profile.environment.namespace,
            profile.environment.context,
            profile.environment.kubeconfig,
        )
    )
    environment_warned = environment_declared and not (kube and kube.available)
    if environment_warned:
        reason = kube.unavailable_reason if kube else NO_CLI_REASON
        report.warnings.append(
            Problem(
                None,
                "environment",
                f"the profile declares an environment but {reason}; "
                "cluster lookups are skipped and selectors cannot be resolved to live pod names",
                remedy="install kubectl or oc, or set --kube-cli to an executable",
            )
        )
    if kube and profile.environment.namespace and kube.available:
        if not kube.can_read_pods(profile.environment.namespace):
            report.warnings.append(
                Problem(
                    None,
                    "environment",
                    f"pods in namespace {profile.environment.namespace!r} are not listable with "
                    f"this kubeconfig/context, so a `setup.requires` selector cannot be "
                    f"resolved to a live pod name",
                    remedy="check --kubeconfig and --context if cluster lookups were expected",
                )
            )

    _check_log_access(
        report, profile, selected, kube, require_logs, environment_warned=environment_warned
    )
    _check_metrics(report, profile)

    for ingress_id in selected:
        ingress = profile.ingress(ingress_id)
        commands = _resolve_setup(ingress, profile, kube)
        report.resolved_setup[ingress_id] = commands

        if ingress.base_url_env and not ingress.base_url:
            report.problems.append(
                Problem(
                    ingress_id,
                    "base_url",
                    f"base_url comes from ${ingress.base_url_env}, which is unset",
                    remedy=f"export {ingress.base_url_env}=https://<gateway host>",
                )
            )
            continue

        if ingress.base_url_env and ingress.transport.http_redirect:
            # The loader checks a literal base_url; a variable's value is checked here. Neither
            # the message nor the remedy quotes the value, which is a hostname.
            reason = plain_http_url(ingress.base_url)[1]
            if reason:
                report.problems.append(
                    Problem(
                        ingress_id,
                        "base_url",
                        f"base_url comes from ${ingress.base_url_env}, but {reason}",
                        remedy=f"export {ingress.base_url_env}=https://<gateway host>, or remove "
                        "transport.http_redirect from an ingress reached over plain HTTP",
                    )
                )

        if ingress.auth.type is not AuthType.NONE and not resolve_secret(ingress):
            report.problems.append(
                Problem(
                    ingress_id,
                    "credentials",
                    f"auth type {ingress.auth.type.value!r} needs "
                    f"${ingress.auth.key_env}, which is unset",
                    remedy=f"export {ingress.auth.key_env}=<key>",
                )
            )
            continue

        proxy_problem = _check_proxy(ingress)
        if proxy_problem:
            report.problems.append(proxy_problem)
            continue

        reachable, elapsed_ms = _reachable(ingress)
        if reachable:
            report.reachable.add(ingress_id)
            report.latency_ms[ingress_id] = elapsed_ms
            _check_access(report, ingress)
        else:
            remedy = "\n            ".join(commands) if commands else None
            report.problems.append(
                Problem(
                    ingress_id,
                    "reachability",
                    f"{ingress.base_url}{ingress.health_path} did not answer",
                    remedy=remedy,
                )
            )
    return report


# ----------------------------------------------------------------- individual checks


def _check_proxy(ingress: Ingress) -> Problem | None:
    """Reject a configuration where a bypass-declared request would still be proxied.

    Only an explicit proxy variable matters. llmdv's own client sets `trust_env=False` for a
    bypass ingress, so the real risk is the pinned SDKs and anything else in-process that
    reads the environment for itself.
    """
    if ingress.setup.proxy is not ProxyMode.BYPASS:
        return None
    names = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")
    proxies = {name: os.environ.get(name) for name in names}
    active = {name: value for name, value in proxies.items() if value}
    if not active:
        return None

    host = urlsplit(ingress.base_url).hostname or ""
    no_proxy = os.environ.get("NO_PROXY", "") + "," + os.environ.get("no_proxy", "")
    entries = {item.strip() for item in no_proxy.split(",") if item.strip()}
    loopback = {"localhost", "127.0.0.1"}
    if host in entries or (host in loopback and entries & loopback):
        return None

    return Problem(
        ingress.id,
        "proxy",
        f"setup.proxy is 'bypass' but {', '.join(sorted(active))} is set and NO_PROXY does not "
        f"cover {host!r}; an unnoticed proxy hop would silently change results",
        remedy=f'export NO_PROXY="$NO_PROXY,{host}"  '
        "(or set setup.proxy: allow if the hop is intended)",
    )


def _reachable(ingress: Ingress) -> tuple[bool, float]:
    """Whether the ingress answers its health path.

    A non-2xx still counts as reachable: LiteLLM's `/health/readiness` legitimately 401s under
    custom_auth, and "something is listening and speaking HTTP" is what preflight needs to
    know. Only a transport error means nothing is there.
    """
    with RecordingClient(
        ingress.base_url,
        connect_timeout=ingress.timeouts.connect,
        request_timeout=min(ingress.timeouts.connect * 3, 15.0),
        trust_env=ingress.setup.proxy is ProxyMode.ALLOW,
        verify=ingress.tls.verify,
    ) as client:
        exchange = client.get(ingress.health_path)
        if exchange.transport_error:
            log.debug("%s unreachable: %s", ingress.id, exchange.transport_error)
            return False, 0.0
        log.debug("%s health -> %s", ingress.id, exchange.status)
        return True, exchange.elapsed_s * 1000


# How many of the listed model ids an access problem quotes, so a gateway serving hundreds
# still gives a readable message.
LISTED_SHOWN = 8


def _check_access(report: Report, ingress: Ingress) -> None:
    """Whether this key can use this ingress's model, from one GET of the model list.

    The health probe deliberately does not authenticate (gateways exempt their health paths),
    so a wrong key or a model the key may not use is otherwise found only by the first case,
    and then by every case after it: a run of hundreds of identical failures that read as the
    fleet's. The model list answers both questions without generating a token. LiteLLM lists
    only the models a key may call; vLLM lists the model it serves.

    Only a clear answer stops the run: a 401 is a rejected key, and a list that does not name
    the profile's model is a model this key cannot reach. Anything else (no list route, a
    403 from a key scoped away from listing, an unparseable body) is a warning: the check could
    not decide, and the cases still decide for themselves.
    """
    model = ingress.model.public_id
    with RecordingClient(
        ingress.base_url,
        default_headers=ingress.auth.header(resolve_secret(ingress)),
        connect_timeout=ingress.timeouts.connect,
        request_timeout=min(ingress.timeouts.connect * 3, 15.0),
        trust_env=ingress.setup.proxy is ProxyMode.ALLOW,
        verify=ingress.tls.verify,
    ) as client:
        exchange = client.get("/v1/models")
    status = exchange.status
    if exchange.transport_error or status is None:
        report.warnings.append(
            Problem(ingress.id, "access", "could not list models to confirm access; not checked")
        )
        return
    if status == 401:
        key = f"${ingress.auth.key_env}" if ingress.auth.key_env else "the key"
        report.problems.append(
            Problem(
                ingress.id,
                "credentials",
                f"{ingress.base_url} rejected {key} (401 on GET /v1/models)",
                remedy=f"check that {key} is the key for this gateway, with no stray whitespace",
            )
        )
        return
    body = exchange.response_body if 200 <= status < 300 else None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        report.warnings.append(
            Problem(
                ingress.id,
                "access",
                f"GET /v1/models answered {status} without a model list; access to {model!r} "
                "was not confirmed before the run",
            )
        )
        return
    listed = [entry.get("id") for entry in data if isinstance(entry, dict) and entry.get("id")]
    if model in listed:
        log.debug("%s lists %s", ingress.id, model)
        return
    shown = ", ".join(str(name) for name in listed[:LISTED_SHOWN])
    more = f" and {len(listed) - LISTED_SHOWN} more" if len(listed) > LISTED_SHOWN else ""
    report.problems.append(
        Problem(
            ingress.id,
            "access",
            f"{model!r} is not among the models this key can use here "
            f"({shown or 'none listed'}{more})",
            remedy="check the profile's model id, or ask the gateway owners to grant it to "
            "this key",
        )
    )


def _check_log_access(
    report: Report,
    profile: Profile,
    selected: list[str],
    kube: KubeContext | None,
    require_logs: bool,
    *,
    environment_warned: bool = False,
) -> None:
    """Settle whether pod logs are collectable, and say so while it still helps.

    Two directions, both worth reporting. Asking for logs that cannot be collected is an
    environment problem and fails here rather than leaving an empty directory behind. Having
    access and declaring no log source is the opposite finding -- evidence left on the table,
    the same shape as a surface that answers but was never declared.
    """
    declared = any(profile.ingress(i).log_sources for i in selected)
    namespace = profile.environment.namespace
    probe: bool | None = None

    if not (kube and kube.available):
        verdict = LogAccess.NOT_DECLARED if not declared else LogAccess.NO_CLUSTER_CLI
    elif not namespace:
        verdict = LogAccess.NOT_DECLARED if not declared else LogAccess.NO_NAMESPACE
    else:
        probe = kube.can_read_pod_logs(namespace)
        if probe is False:
            verdict = LogAccess.DENIED
        elif probe is None:
            verdict = LogAccess.UNKNOWN
        else:
            verdict = LogAccess.AVAILABLE if declared else LogAccess.NOT_DECLARED

    report.log_access = verdict
    report.log_access_reason = reason = (
        kube.unavailable_reason
        if verdict is LogAccess.NO_CLUSTER_CLI and kube is not None
        else verdict.reason
    )

    # Asked for every layer's logs and cannot have them. Exit 3: the run would otherwise spend
    # its whole length producing a bundle that is missing the thing it was told to capture.
    if require_logs and declared and verdict is not LogAccess.AVAILABLE:
        report.problems.append(
            Problem(
                None,
                "environment",
                f"--pod-logs all was requested and log sources are declared, but {reason}",
                remedy="re-run with --pod-logs failures, or fix cluster access",
            )
        )
        return

    if verdict is LogAccess.NOT_DECLARED and probe is True:
        report.warnings.append(
            Problem(
                None,
                "environment",
                f"pod logs are readable in namespace {namespace!r}, but no selected ingress "
                f"declares a log source, so the run will carry no second account of a failure",
                remedy="add a `logs:` block to an ingress to collect it",
            )
        )
    elif (
        declared
        and verdict is not LogAccess.AVAILABLE
        and not (verdict is LogAccess.NO_CLUSTER_CLI and environment_warned)
    ):
        report.warnings.append(
            Problem(None, "environment", f"no pod logs will be collected: {reason}")
        )


def _check_metrics(report: Report, profile: Profile) -> None:
    """A declared metrics source answers one instant query, with its credential.

    Checked here for the same reason reachability is: a dead port-forward to the metrics API
    would otherwise fail every metrics case as a fleet defect.
    """
    from .fleet.prometheus import MetricsError, PrometheusReader, token_for

    source = profile.environment.metrics
    if source is None:
        return
    token = token_for(source)
    if source.key_env and not token:
        report.problems.append(
            Problem(
                None,
                "credentials",
                f"environment.metrics needs ${source.key_env}, which is unset",
                remedy=f"export {source.key_env}=<token>",
            )
        )
        return
    try:
        PrometheusReader(source, token).query("vector(1)")
    except MetricsError as exc:
        report.problems.append(
            Problem(
                None,
                "environment",
                f"metrics source {source.url} did not answer an instant query: {exc}",
                remedy="start the port-forward or tunnel to it, or remove environment.metrics",
            )
        )


def _resolve_setup(ingress: Ingress, profile: Profile, kube: KubeContext | None) -> list[str]:
    """Turn declared requirements into commands that work right now.

    This is what a kubeconfig buys. A requirement declared by selector is resolved to a live
    pod name, so the printed command is runnable rather than a template with a pod name that
    was accurate when the profile was written.
    """
    commands: list[str] = []
    for requirement in ingress.setup.requires:
        commands.append(_command_for(requirement, profile, kube))
    return commands


def _command_for(requirement: Requirement, profile: Profile, kube: KubeContext | None) -> str:
    if requirement.command:
        return requirement.command

    namespace = requirement.namespace or profile.environment.namespace or "<namespace>"
    target = requirement.target

    if not target and requirement.selector:
        target = f"-l {requirement.selector}"
        if kube and kube.available:
            try:
                pods = kube.pods_for_selector(requirement.selector, namespace)
                if pods:
                    target = f"pod/{pods[0]}"
                else:
                    return (
                        f"# no Ready pod matches {requirement.selector!r} in {namespace} -- "
                        f"the workload may be down, which is a fleet problem, not a setup one"
                    )
            except KubeError as exc:
                log.debug("selector resolution failed: %s", exc)

    base = (kube or KubeContext(context=profile.environment.context)).display_prefix()
    ports = f" {requirement.local_port}:{requirement.remote_port}"
    return f"{shlex.join(base)} -n {shlex.quote(namespace)} port-forward {target}{ports}"
