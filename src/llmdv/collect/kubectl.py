"""Pod logs via the read-only kubectl/oc adapter.

Collected ONCE per run, for the whole run window, rather than per failure. Five failures
against the same decode pod would otherwise fetch five near-identical copies of the same log;
one windowed collection, referenced from every failure, is both cheaper and easier to read.

Everything written here goes through the same redaction as the rest of the evidence. A vLLM
log is unlikely to hold a credential, but a LiteLLM log at debug can, and the run directory is
what `--remote-output` publishes.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..http import redact
from ..kube import KubeContext, KubeError
from ..log.setup import get_logger
from ..output.offload import Offloader, reference
from ..profile.schema import LogSource, Profile
from .base import CollectedLog, Collection

log = get_logger("collect.kubectl")

# Per source. A decode deployment of three replicas is worth collecting whole, since the router
# may have used any of them; a fleet of ninety is not, and the cap is recorded when it bites.
MAX_PODS_PER_SOURCE = 5


class KubectlLogCollector:
    def __init__(
        self, kube: KubeContext, namespace: str | None = None, offloader: Offloader | None = None
    ) -> None:
        self.kube = kube
        self.namespace = namespace
        self.offloader = offloader

    def describe(self) -> str:
        return f"{self.kube.binary or 'cluster CLI'} logs (namespace {self.namespace or '-'})"

    def collect(
        self,
        profile: Profile,
        ingress_ids: list[str],
        since_time: str | None,
        out_dir: Path,
    ) -> Collection:
        collection = Collection()
        if not self.kube.available:
            collection.reason = self.kube.unavailable_reason
            log.debug("%s; skipping log collection", collection.reason)
            return collection

        namespace = self.namespace or profile.environment.namespace
        sources = [(i, source) for i in ingress_ids for source in profile.ingress(i).log_sources]
        if not sources:
            collection.reason = "no selected ingress declares a log source"
            return collection
        # Created only now that there is something to put in it.
        out_dir.mkdir(parents=True, exist_ok=True)

        for ingress_id, source in sources:
            collection.logs.extend(
                self.collect_source(ingress_id, source, namespace, since_time, out_dir)
            )
        if not collection.logs:
            collection.reason = "no pod matched any declared log source"
        elif not collection.ok:
            collection.reason = "all pod log reads failed"
        return collection

    def collect_source(
        self,
        ingress_id: str,
        source: LogSource,
        namespace: str | None,
        since_time: str | None,
        out_dir: Path,
        complete: bool = False,
    ) -> list[CollectedLog]:
        """One declared source's containers, each windowed and written under `out_dir`.

        The log-signature cases read through this, so they scan what a run collects.
        `complete` reads every Running pod (Ready or not) with no pod cap and no line cap:
        a scan that saw five pods or the last 2000 lines cannot call the rest clean.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        logs = []
        if complete:
            source = replace(source, tail=-1)
        for pod in self._pods_for(source, namespace, complete):
            containers = (
                [source.container]
                if source.container
                else self.kube.containers_in(pod, namespace) or [None]
            )
            for container in containers:
                logs.append(
                    self._one(ingress_id, pod, container, source, namespace, since_time, out_dir)
                )
        return logs

    def collect_previous(
        self,
        ingress_id: str,
        pod: str,
        container: str,
        source: LogSource,
        namespace: str | None,
        since_time: str | None,
        out_dir: Path,
    ) -> CollectedLog:
        """The last terminated instance of one container, complete, for a restart in the window."""
        return self._one(
            ingress_id,
            pod,
            container,
            replace(source, tail=-1),
            namespace,
            since_time,
            out_dir,
            previous=True,
        )

    # ----------------------------------------------------------------- internals

    def _pods_for(
        self, source: LogSource, namespace: str | None, complete: bool = False
    ) -> list[str]:
        """Resolve a log source to concrete pod names."""
        try:
            if source.selector and complete:
                pods = self.kube.pods_for_selector(source.selector, namespace, ready_only=False)
            elif source.selector:
                pods = self.kube.pods_for_selector(source.selector, namespace)
            elif source.target and source.target.startswith("pod/"):
                pods = [source.target.split("/", 1)[1]]
            elif source.target and source.target.startswith(("svc/", "service/")):
                name = source.target.split("/", 1)[1]
                selector = (
                    self.kube.service_selector(name, namespace, strict=True)
                    if complete
                    else self.kube.service_selector(name, namespace)
                )
                if not selector:
                    pods = []
                elif complete:
                    pods = self.kube.pods_for_selector(selector, namespace, ready_only=False)
                else:
                    pods = self.kube.pods_for_selector(selector, namespace)
            else:
                pods = []
        except KubeError as exc:
            log.debug("could not resolve pods for %s: %s", source, exc)
            if complete:
                raise
            return []

        if len(pods) > MAX_PODS_PER_SOURCE and not complete:
            log.info(
                "collecting logs from %d of %d pods matching %s",
                MAX_PODS_PER_SOURCE,
                len(pods),
                source.selector or source.target,
            )
            pods = pods[:MAX_PODS_PER_SOURCE]
        return pods

    def _one(
        self,
        ingress_id: str,
        pod: str,
        container: str | None,
        source: LogSource,
        namespace: str | None,
        since_time: str | None,
        out_dir: Path,
        previous: bool = False,
    ) -> CollectedLog:
        entry = CollectedLog(ingress_id=ingress_id, pod=pod, container=container or "default")
        offload_path = (
            self.offloader is not None
            and self.offloader.uri is not None
            and source.tail > 0
            and not previous
        )
        fallback_offload: dict | None = None
        try:
            text = self.kube.pod_logs(
                pod,
                container=container,
                namespace=namespace,
                since_time=since_time,
                tail=-1 if offload_path else source.tail,
                **({"previous": True} if previous else {}),
            )
        except KubeError as exc:
            if offload_path:
                fallback_offload = {
                    "status": "failed",
                    "error": redact.scrub(str(exc))[:300],
                }
                try:
                    text = self.kube.pod_logs(
                        pod,
                        container=container,
                        namespace=namespace,
                        since_time=since_time,
                        tail=source.tail,
                    )
                except KubeError as fallback_exc:
                    entry.error = str(fallback_exc)[:300]
                    log.debug("log collection failed for %s/%s: %s", pod, container, fallback_exc)
                    return entry
            else:
                entry.error = str(exc)[:300]
                log.debug("log collection failed for %s/%s: %s", pod, container, exc)
                return entry

        text = redact.scrub(text)
        kept = text
        lines = text.count("\n")
        name = (
            f"{ingress_id}__{pod}"
            + (f"__{container}" if container else "")
            + ("__previous" if previous else "")
            + ".log"
        )
        if offload_path and lines > source.tail:
            kept = "".join(text.splitlines(keepends=True)[-source.tail :])
            data = text.encode("utf-8")
            offload = self.offloader.store(f"logs/{name}", data)
            entry.artifact = reference(data, len(kept.encode("utf-8")), offload)
            entry.truncated = True
        elif fallback_offload is not None:
            entry.artifact = {
                "schema": "llmdv.artifact/1",
                "bytes": None,
                "sha256": None,
                "kept_bytes": len(kept.encode("utf-8")),
                "truncated": 0 < source.tail <= lines,
                "offload": fallback_offload,
            }
            entry.truncated = 0 < source.tail <= lines
        (out_dir / name).write_text(kept, encoding="utf-8")

        entry.path = f"logs/{name}"
        entry.lines = lines
        if not entry.truncated:
            entry.truncated = 0 < source.tail <= lines and not offload_path
        return entry
