"""A read-only kubectl/oc adapter.

llmdv exists to test deployed infrastructure, never to change it, so this module can only
read: every invocation goes through `_run`, which refuses any verb outside `READ_VERBS`. That
is a structural guarantee rather than a convention -- there is no code path from llmdv to
`apply`, `delete`, `scale` or `exec`.

What a kubeconfig buys, concretely: a profile declares a port-forward requirement by
*selector* rather than by pod name, because pod names rot: a decode pod carries a generated
suffix that changes on every reschedule. With cluster read access llmdv
resolves the selector to a live name and prints a command that works now; without it, it
falls back to the selector text and says so. It never runs the forward itself.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .log.setup import get_logger

log = get_logger("kube")

# Everything llmdv is permitted to do to a cluster. `logs` reads a container's stdout and
# changes nothing, so it belongs here; the allowlist exists to keep `apply`, `delete`, `scale`
# and `exec` unreachable, not to keep llmdv from reading.
READ_VERBS = frozenset({"get", "version", "config", "logs"})

DEFAULT_TIMEOUT = 20.0
NO_CLI_REASON = "neither kubectl nor oc is on PATH"

# A pod name no real workload should carry. Access probes expect NotFound; log reads are
# bounded to one line in case a pod really does use this name.
LOG_PROBE_POD = "llmdv-access-probe"


class KubeError(Exception):
    pass


@dataclass(frozen=True)
class KubeContext:
    """Which cluster to read, and with what credentials.

    All fields are optional. Cluster credentials default to the CLI's own configuration,
    including the mounted ServiceAccount in-cluster. Resolve the executable once: an explicit
    cli wins, otherwise prefer kubectl and then oc. An unavailable override never falls back
    to another executable, which could address a different cluster.
    """

    kubeconfig: Path | None = None
    context: str | None = None
    namespace: str | None = None
    cli: str | None = None
    binary: str | None = field(init=False)

    def __post_init__(self) -> None:
        candidates = (self.cli,) if self.cli is not None else ("kubectl", "oc")
        binary = next((path for name in candidates if (path := shutil.which(name))), None)
        object.__setattr__(self, "binary", str(Path(binary).absolute()) if binary else None)
        log.debug("cluster CLI: %s", self.binary or self.unavailable_reason)

    @property
    def unavailable_reason(self) -> str:
        if self.cli is not None:
            return f"requested cluster CLI {self.cli!r} is not executable or not on PATH"
        return NO_CLI_REASON

    @property
    def available(self) -> bool:
        return self.binary is not None

    def display_prefix(self) -> list[str]:
        """Portable setup prefix; the operator supplies their own KUBECONFIG.

        Preserve an explicit CLI override exactly, otherwise use the resolved basename.
        Execution still uses the absolute binary and kubeconfig through `_base`.
        """
        name = self.cli if self.cli is not None else Path(self.binary or "kubectl").name
        return [name, "--context", self.context] if self.context else [name]

    def _base(self) -> list[str]:
        if self.binary is None:
            raise KubeError(self.unavailable_reason)
        args = [self.binary]
        if self.kubeconfig:
            args += ["--kubeconfig", str(self.kubeconfig)]
        if self.context:
            args += ["--context", self.context]
        return args

    def _run(self, verb: str, *rest: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        if verb not in READ_VERBS:
            raise KubeError(
                f"refusing verb {verb!r}: llmdv is read-only "
                f"(permitted: {', '.join(sorted(READ_VERBS))})"
            )
        if not self.available:
            raise KubeError(self.unavailable_reason)
        args = [*self._base(), verb, *rest]
        log.debug("cluster CLI: %s", " ".join(args))
        try:
            done = subprocess.run(  # noqa: S603 -- resolved binary, verb allowlisted above
                args, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise KubeError(f"{self.binary} timed out after {timeout}s: {' '.join(args)}") from exc
        except OSError as exc:
            raise KubeError(f"could not run {self.binary}: {exc}") from exc
        if done.returncode != 0:
            raise KubeError((done.stderr or done.stdout or f"{self.binary} failed").strip())
        return done.stdout

    # ------------------------------------------------------------------ reads

    def can_read_pods(self, namespace: str) -> bool:
        """Whether pods in `namespace` can be listed, which selector resolution needs.

        Namespaced on purpose: a namespaced Role can never grant reading the Namespace itself.
        The field selector matches nothing, so the check returns an empty list cheaply.

        Not a proxy for log access -- see `can_read_pod_logs`, which probes the other verb.
        """
        try:
            self._run(
                "get", "pods", "-n", namespace, "-o", "name",
                "--field-selector", f"metadata.name={LOG_PROBE_POD}",
            )  # fmt: skip
            return True
        except KubeError as exc:
            log.debug("pods in namespace %s not readable: %s", namespace, exc)
            return False

    def can_read_pod_logs(self, namespace: str) -> bool | None:
        """Whether `pods/log` is permitted in `namespace`. None when the probe is inconclusive.

        A separate check from `can_read_pods` because they are separate RBAC resources: a Role
        granting `list` on pods does not grant `get` on `pods/log`, so selector resolution can
        work while every log read 403s. Probing the wrong verb is how a run announces that it
        is collecting logs and then collects none.

        GET the log subresource directly: `kubectl logs` first looks up the Pod, whose 404
        says nothing about log permission. The probe pod should not exist, so this costs one
        read. NotFound means log access is permitted and only the pod is missing;
        Forbidden means it is not. Anything else -- an unreachable cluster, an expired token --
        is inconclusive, and returning None to say so beats guessing `True` and producing an
        empty logs/ directory, or guessing `False` and skipping logs that were available.
        """
        ns = quote(namespace, safe="")
        try:
            self._run(
                "get",
                "--raw",
                f"/api/v1/namespaces/{ns}/pods/{LOG_PROBE_POD}/log?tailLines=1",
            )
        except KubeError as exc:
            text = str(exc).lower()
            if "forbidden" in text:
                return False
            if "notfound" in text or "not found" in text:
                return True
            log.debug("pod log access in %s is inconclusive: %s", namespace, exc)
            return None
        # A pod really is called this, and we read it. Unlikely, but it answers the question.
        return True

    def pods_for_selector(
        self, selector: str, namespace: str | None = None, ready_only: bool = True
    ) -> list[str]:
        """Ready pod names matching a label selector, most recently started first.

        Only Ready pods are returned: a Pending or Terminating pod is a port-forward target
        that fails in a way that looks like the fleet is broken. `ready_only=False` keeps every
        Running pod, for a log scan that must also read a pod that just failed readiness.
        """
        ns = namespace or self.namespace
        if not ns:
            raise KubeError("no namespace given and none set on the context")
        raw = self._run("get", "pods", "-n", ns, "-l", selector, "-o", "json")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KubeError(f"could not parse {self.binary} output: {exc}") from exc

        ready: list[str] = []
        for item in payload.get("items", []):
            status = item.get("status") or {}
            if status.get("phase") != "Running":
                continue
            conditions = {c.get("type"): c.get("status") for c in (status.get("conditions") or [])}
            if ready_only and conditions.get("Ready") != "True":
                continue
            ready.append((item.get("metadata") or {}).get("name", ""))
        return [name for name in ready if name]

    def resource_exists(self, kind: str, name: str, namespace: str | None = None) -> bool:
        ns = namespace or self.namespace
        try:
            args = ["get", kind, name, "-o", "name"]
            if ns:
                args += ["-n", ns]
            self._run(*args)
            return True
        except KubeError:
            return False

    def service_selector(
        self, name: str, namespace: str | None = None, *, strict: bool = False
    ) -> str | None:
        """The label selector a Service routes to, as a `k=v,k=v` string.

        A profile usually names an ingress by its Service, but logs come from pods, so the
        Service's own selector is the bridge between the two.
        """
        ns = namespace or self.namespace
        args = ["get", "service", name, "-o", "json"]
        if ns:
            args += ["-n", ns]
        try:
            spec = (json.loads(self._run(*args)).get("spec") or {}).get("selector") or {}
        except (KubeError, json.JSONDecodeError) as exc:
            if strict:
                raise KubeError(f"could not read selector for service {name}: {exc}") from exc
            log.debug("could not read selector for service %s: %s", name, exc)
            return None
        return ",".join(f"{k}={v}" for k, v in sorted(spec.items())) or None

    def containers_in(self, pod: str, namespace: str | None = None) -> list[str]:
        ns = namespace or self.namespace
        args = ["get", "pod", pod, "-o", 'jsonpath={range .spec.containers[*]}{.name}{"\\n"}{end}']
        if ns:
            args += ["-n", ns]
        try:
            return [line for line in self._run(*args).splitlines() if line.strip()]
        except KubeError as exc:
            log.debug("could not list containers in %s: %s", pod, exc)
            return []

    def pod_logs(
        self,
        pod: str,
        container: str | None = None,
        namespace: str | None = None,
        since_time: str | None = None,
        tail: int = 2000,
        timeout: float = 60.0,
        previous: bool = False,
    ) -> str:
        """Container logs, windowed to the run.

        `--since-time` rather than the whole history: a vLLM pod that has been up for days
        would otherwise bury the handful of lines that belong to this run, and the point of
        collecting at all is to put the engine's own account next to a failed request.
        """
        ns = namespace or self.namespace
        args = ["logs", pod, f"--tail={tail}"]
        if container:
            args += ["-c", container]
        if ns:
            args += ["-n", ns]
        if since_time:
            args += [f"--since-time={since_time}"]
        if previous:
            # The container instance before the last restart: where a crash wrote its last words.
            args.append("--previous")
        return self._run(*args, timeout=timeout)

    def get_objects(
        self,
        kind: str,
        name: str | None = None,
        selector: str | None = None,
        namespace: str | None = None,
    ) -> list[dict]:
        """Objects of one kind, read with `get -o json`: the named one, or every match.

        The configuration checks read through this and nothing else, so they share the verb
        allowlist with every other cluster read. A named object that does not exist raises
        KubeError carrying kubectl's NotFound text; callers decide what an absence means.
        """
        ns = namespace or self.namespace
        args = ["get", kind]
        if name:
            args.append(name)
        if ns:
            args += ["-n", ns]
        if selector:
            args += ["-l", selector]
        raw = self._run(*args, "-o", "json")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KubeError(f"could not parse {self.binary} output: {exc}") from exc
        if not isinstance(payload, dict):
            raise KubeError(f"{self.binary} returned something other than an object")
        if name:
            return [payload]
        return [item for item in payload.get("items") or [] if isinstance(item, dict)]

    def current_context(self) -> str | None:
        try:
            return self._run("config", "current-context").strip() or None
        except KubeError:
            return None
