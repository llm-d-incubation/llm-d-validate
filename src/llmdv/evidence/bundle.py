"""Per-failure evidence: enough to reproduce the failure without the original terminal.

Everything written here is already redacted -- the recording client redacts at capture time,
so there is no point at which an unredacted body is serialised. The generated `repro.sh` reads
any credential from the environment variable the profile names, which makes the whole
directory shareable as-is.

`meta.json` carries the resolved setup commands on purpose: the same `base_url` means
different things through a port-forward and in-cluster, so a bundle without the vantage point
recorded is not actually reproducible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..http import redact
from ..http.client import Exchange
from ..http.repro import to_curl
from ..log.setup import get_logger
from ..output.offload import whole
from ..profile.schema import Ingress

log = get_logger("evidence")


def _text(path: Path, text: str) -> None:
    """Write text evidence, scrubbed: an assertion or transcript can quote what came back."""
    path.write_text(redact.scrub(text), encoding="utf-8")


def _json(path: Path, value: Any) -> None:
    """Write JSON evidence with every string scrubbed, so the file stays valid JSON."""
    path.write_text(json.dumps(redact.redact_value(value), indent=2), encoding="utf-8")


@dataclass
class FailureContext:
    nodeid: str
    case_id: str | None
    ingress: Ingress | None
    surface: str | None
    exchanges: list[Exchange] = field(default_factory=list)
    served_by: dict[str, Any] = field(default_factory=dict)
    setup_commands: list[str] = field(default_factory=list)
    captured_output: str = ""
    longrepr: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = "fail"
    # The non-ingress parametrisation. Without it the six header-disposition cases share two
    # directory names and silently overwrite each other, so a 22-case run writes 18 bundles.
    variant: str | None = None
    # Files a case contributed itself, by published name. For evidence that belongs beside the
    # verdict rather than inside a check -- a client's event trace, the project it edited.
    artifacts: dict[str, str] = field(default_factory=dict)
    # What the profile's `known_failures` said about this result, if anything.
    known_failure: dict[str, Any] | None = None


def slug(text: str) -> str:
    keep = [char if (char.isalnum() or char in "._-") else "_" for char in text]
    return "".join(keep).strip("_")[:120]


def case_slug(ingress_id: str, case: str, variant: str | None) -> str:
    if variant:
        case = f"{case}__{variant}"
    return f"{slug(ingress_id)}/{slug(case)}"


def write(run_dir: Path, context: FailureContext) -> str:
    """Write one failure directory and return its path relative to the run directory."""
    where = context.ingress.id if context.ingress else "noingress"
    case = context.case_id or context.nodeid
    bucket = "failures" if context.outcome in ("fail", "error") else "cases"
    # Nested by ingress, not prefixed with it: `ls failures/` then answers which LAYER broke
    # before you read a single case name, and `failures/<layer>/` isolates one. Always nested,
    # even for a single-ingress run -- a path you can predict without knowing the run's shape
    # is worth more than saving a level.
    relative = Path(bucket) / case_slug(where, case, context.variant)
    target = run_dir / relative
    target.mkdir(parents=True, exist_ok=True)

    # The exchange that decided the case is the last one; earlier ones are setup and still
    # useful, so they are kept in exchanges.json alongside.
    primary = context.exchanges[-1] if context.exchanges else None
    transcript_exchange = next(
        (
            exchange
            for exchange in context.exchanges
            if exchange.transcript_bytes is not None and exchange.response_text
        ),
        None,
    )
    transcript_ref = transcript_exchange.transcript_ref if transcript_exchange else None
    refs = {"transcript.sse": transcript_ref} if transcript_ref is not None else {}
    partial_evidence = any(
        exchange.transcript_ref is not None and not whole(exchange.transcript_ref)
        for exchange in context.exchanges
    )

    # verdict.txt is the human entry point: what was claimed, and the observations behind it.
    # A bundle written for a PASS exists precisely so the claim can be checked, so leading with
    # the checks rather than the raw JSON is the whole point.
    verdict = [
        f"case     {context.case_id or context.nodeid}",
        f"ingress  {context.ingress.id if context.ingress else '-'}"
        f"  ({context.ingress.base_url if context.ingress else '-'})",
        f"surface  {context.surface or '-'}",
        f"outcome  {context.outcome.upper()}",
    ]
    known = context.known_failure or {}
    if known.get("status") == "known":
        fix = f"; fix: {known['fix']}" if known.get("fix") else ""
        verdict.append(f"known    {known.get('issue')} (declared in the profile{fix})")
    elif known.get("status") == "fix_expected":
        seen = ", ".join(f"{k}={v}" for k, v in (known.get("observed") or {}).items())
        verdict.append(
            f"known    {known.get('issue')}: the fix was expected on this build ({seen}) "
            "and the case still fails"
        )
    verdict += ["", "checks"]
    if context.checks:
        for check in context.checks:
            mark = "ok    " if check.get("ok") else "FAILED"
            against = f"   expected {check['expected']}" if check.get("expected") else ""
            verdict.append(f"  {mark} {check['name']} = {check['observed']!r}{against}")
    else:
        verdict.append("  (none recorded -- this case asserted nothing through the helpers)")
    if context.outcome in ("fail", "error") and all(c.get("ok") for c in context.checks):
        verdict.append(
            "  NOTE: the verdict is a failure but no recorded check failed, so the deciding"
        )
        verdict.append(
            "        assertion did not record one. See assertion.txt for what actually fired."
        )
    verdict += ["", f"exchanges  {len(context.exchanges)}"]
    for index, exchange in enumerate(context.exchanges):
        verdict.append(
            f"  [{index}] {exchange.method} {exchange.url} -> "
            f"{exchange.status if exchange.status is not None else exchange.transport_error}"
            f"  ({exchange.elapsed_s:.3f}s)"
        )
    if partial_evidence:
        verdict.append(
            "evidence  partial: the local transcript is capped and no whole copy was stored"
        )
    elif transcript_ref is not None:
        verdict.append("evidence  whole transcript offloaded; local copy is capped")
    _text(target / "verdict.txt", "\n".join(verdict) + "\n")

    if context.checks:
        _json(target / "checks.json", context.checks)

    if primary is not None:
        _json(
            target / "request.json",
            {
                "method": primary.method,
                "url": primary.url,
                "headers": primary.request_headers,
                "body": primary.request_body,
            },
        )
        _json(
            target / "response.json",
            {
                "status": primary.status,
                "headers": primary.response_headers,
                "body": primary.response_body
                if primary.response_body is not None
                else primary.response_text,
                "transport_error": primary.transport_error,
            },
        )
        # Literal secrets only: the script's own "Bearer ${KEY}" is what makes it replayable.
        (target / "repro.sh").write_text(
            redact.redact_text(
                to_curl(
                    primary,
                    auth=context.ingress.auth if context.ingress else None,
                    setup_hint=context.setup_commands,
                )
            ),
            encoding="utf-8",
        )
        (target / "repro.sh").chmod(0o755)

    # Whatever the case attached itself. Named by the case, slugged here, and never allowed to
    # escape the bundle directory.
    for name, text in context.artifacts.items():
        safe = slug(name) or "artifact"
        _text(target / safe, text)

    # transcript.sse for streamed cases. An SSE body is not JSON, so response.json holds it as
    # text; writing it under its own name makes it readable and greppable as a stream.
    if transcript_exchange is not None:
        exchange = transcript_exchange
        note = ""
        if exchange.transcript_truncated:
            offload = (exchange.transcript_ref or {}).get("offload", {})
            status = offload.get("status", "not_configured")
            detail = offload.get("uri") or offload.get("error", status)
            note = f"# [llmdv] transcript truncated for storage; whole copy: {detail}\n"
        _text(target / "transcript.sse", note + exchange.response_text)

    if context.exchanges:
        _json(target / "exchanges.json", [item.to_dict() for item in context.exchanges])

    meta = {
        "nodeid": context.nodeid,
        "case_id": context.case_id,
        "ingress": context.ingress.id if context.ingress else None,
        "ingress_kind": context.ingress.kind.value if context.ingress else None,
        "base_url": context.ingress.base_url if context.ingress else None,
        "surface": context.surface,
        "served_by": context.served_by,
        "setup_commands": context.setup_commands,
        "elapsed_s": round(primary.elapsed_s, 4) if primary else None,
        "partial_evidence": partial_evidence,
    }
    if refs:
        meta["artifacts"] = refs
    _json(target / "meta.json", meta)

    if context.captured_output.strip():
        _text(target / "captured_output.txt", context.captured_output)
    if context.longrepr.strip():
        _text(target / "assertion.txt", context.longrepr)

    log.debug("evidence written: %s", target)
    return str(relative)
