"""Forbidden log signatures in the run window, one result per declared log source.

Some failures leave no trace on the wire: a decode that read KV the prefill engine released, a
rejected KV event batch, a scheduler assertion, a validation error logged with its whole stack.
A log source may declare such lines under `forbid`, and lines that excuse a match under
`allow`. This phase runs last, so it scans everything the run produced, through the same
read-only collector that gathers logs for failure evidence.

The scan is complete for the window: every Running pod of the source, Ready or not, every line
since the run began, and the previous instance of any container that restarted during the run,
which is where a crash writes its last lines. A hit is attributed to the llmdv case whose request
id or response id appears on the line. A hit with no llmdv id still fails: the window is this
run's, and the fleet logged the signature. Every case asserts it read logs from at least one
pod and that every read succeeded, so an empty or denied read never passes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ...assertions import fail
from ...collect import KubectlLogCollector
from ...evidence import journal
from ...http.redact import scrub
from ...kube import KubeError
from ...profile.schema import SkipClass
from .helpers import absent_error, instrumentation_error, require_kubectl

# Shorter ids would match unrelated text.
_MIN_ID = 8


def _run_ids(run_dir: Path | None) -> dict[str, str]:
    """Request and response ids this run sent or received, mapped to the case that did."""
    if run_dir is None or not (run_dir / "exchanges.jsonl").is_file():
        return {}
    found: dict[str, str] = {}
    for line in (run_dir / "exchanges.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            exchange = json.loads(line)
        except json.JSONDecodeError:
            continue
        case = exchange.get("case_id") or exchange.get("nodeid") or "?"
        headers = {str(k).lower(): v for k, v in (exchange.get("request_headers") or {}).items()}
        body = exchange.get("response_body")
        for value in (
            headers.get("x-request-id"),
            body.get("id") if isinstance(body, dict) else None,
        ):
            if isinstance(value, str) and len(value) >= _MIN_ID:
                found[value] = case
    return found


def _scan(text: str, source, ids: dict[str, str]) -> list[dict]:
    hits = []
    for line in text.splitlines():
        for signature in source.forbid:
            if not re.search(signature.pattern, line):
                continue
            if any(re.search(pattern, line) for pattern in source.allow):
                continue
            case = next((owner for token, owner in ids.items() if token in line), None)
            hits.append({"signature": signature.id, "line": scrub(line[:300]), "llmdv_case": case})
    return hits


def _restarted(kube, collected, namespace, since, selected: str | None):
    """Containers whose previous instance ended inside the window: a crash's last lines."""
    found = []
    errors = {}
    for pod in sorted({entry.pod for entry in collected}):
        try:
            status = (kube.get_objects("pods", pod, namespace=namespace)[0].get("status")) or {}
        except (KubeError, IndexError) as exc:
            errors[pod] = str(exc)
            continue
        for item in status.get("containerStatuses") or []:
            if selected and item.get("name") != selected:
                continue
            ended = ((item.get("lastState") or {}).get("terminated") or {}).get("finishedAt")
            if item.get("restartCount") and ended and (since is None or ended >= since):
                found.append((pod, str(item.get("name"))))
    return found, errors


@pytest.mark.case_id("logs.signatures")
def test_signatures(request, profile, log_source, fleet_kube, tmp_path):
    """No declared forbidden signature appears in a source's logs for the run window."""
    ingress = profile.ingress(log_source.ingress_id)
    source = ingress.logs[log_source.index]
    journal.record("source", log_source.label)
    require_kubectl(fleet_kube)
    since = request.config.getoption("--llmdv-started-at") or getattr(
        request.config, "_llmdv_log_window", None
    )
    run_dir: Path | None = getattr(request.config, "_llmdv_run_dir", None)
    out_dir = (run_dir / "log-scan") if run_dir else tmp_path
    namespace = profile.environment.namespace
    collector = KubectlLogCollector(fleet_kube, namespace)
    try:
        collected = collector.collect_source(
            ingress.id, source, namespace, since, out_dir, complete=True
        )
    except KubeError as exc:
        if absent_error(exc):
            fail("resolve log source", str(exc)[:300], "the declared source exists")
        if instrumentation_error(exc):
            pytest.skip(
                f"[{SkipClass.REQUIRES_INSTRUMENTATION.value}] could not resolve log source: "
                f"{str(exc)[:200]}"
            )
        fail("resolve log source", str(exc)[:300], "a valid source that can be resolved")
    restarted, status_errors = _restarted(fleet_kube, collected, namespace, since, source.container)
    journal.record("restarted in the window", restarted or "none")
    for pod, container in restarted:
        collected.append(
            collector.collect_previous(
                ingress.id, pod, container, source, namespace, since, out_dir
            )
        )

    pods = sorted({entry.pod for entry in collected})
    journal.record("pods read", pods, "at least one", ok=bool(pods))
    if not pods:
        fail("pods read", "none", "at least one pod for the source; an empty read is not clean")
    errors = {
        **{f"status {pod}": error for pod, error in status_errors.items()},
        **{f"{e.pod}/{e.container}": e.error for e in collected if e.error},
    }
    if errors and all(instrumentation_error(str(text)) for text in errors.values()):
        pytest.skip(
            f"[{SkipClass.REQUIRES_INSTRUMENTATION.value}] cluster logs could not be read: "
            f"{next(iter(errors.values()))[:200]}"
        )
    journal.record("reads failed", errors or "none", "none", ok=not errors)
    if errors:
        fail("reads failed", errors, "logs read from every pod and container")

    ids = _run_ids(run_dir)
    hits = []
    for entry in collected:
        text = (out_dir / Path(entry.path or "").name).read_text(encoding="utf-8")
        hits += [{"pod": entry.pod, **hit} for hit in _scan(text, source, ids)]
    journal.record("window", {"since": since, "lines": sum(entry.lines for entry in collected)})
    attributed = sorted({hit["llmdv_case"] for hit in hits if hit["llmdv_case"]})
    journal.record("llmdv cases on hit lines", attributed or "none")
    journal.record(
        "forbidden signatures",
        hits[:20] or "none",
        f"none of {[signature.id for signature in source.forbid]}",
        ok=not hits,
    )
    if hits:
        counts: dict[str, int] = {}
        for hit in hits:
            counts[hit["signature"]] = counts.get(hit["signature"], 0) + 1
        fail("forbidden signatures", {"counts": counts, "first": hits[0]}, "none in the run window")
